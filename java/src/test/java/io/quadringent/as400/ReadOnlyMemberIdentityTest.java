package io.quadringent.as400;

import com.ibm.as400.access.AS400Text;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

/** Native byte fixtures and JDBC doubles only; no IBM i connection. */
public final class ReadOnlyMemberIdentityTest {
    private static final int CCSID = 37;
    private static final String SHA = "a".repeat(64);
    private static final String JID = "010203040506070809ff";
    private static final BigInteger LARGE = new BigInteger("9223372036854775808");
    private static final BigInteger MAX = new BigInteger("999999999999999999999");
    private static final Instant NOW = Instant.parse("2026-01-01T00:00:00Z");
    private static int checks;

    private ReadOnlyMemberIdentityTest() { }

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && args[0].equals("--emit-fixture")) {
            System.out.write(collect(new Fake()).metadata());
            return;
        }
        exactIdentityAndCalls();
        inputAndSequenceBounds();
        nativeFailures();
        jdbcFailures();
        budgetsAndOutput();
        cliAndTls();
        System.out.println("read_only_member_identity=PASS checks=" + checks);
    }

    private static ReadOnlyMemberIdentity.Request request(BigInteger start, BigInteger end) {
        return new ReadOnlyMemberIdentity.Request("TESTLIB", "JRN", "RCVLIB", "RCV9",
                "TESTLIB", "ITEMS", "ITEMS", start, end, SHA);
    }

    private static ReadOnlyMemberIdentity.Result collect(Fake fake) throws Exception {
        return collect(fake, request(LARGE, MAX), ReadOnlyMemberIdentity.Limits.defaults());
    }

    private static ReadOnlyMemberIdentity.Result collect(Fake fake, ReadOnlyMemberIdentity.Request request,
            ReadOnlyMemberIdentity.Limits limits) throws Exception {
        return ReadOnlyMemberIdentity.collect(fake.connection(), fake::nativeCall, CCSID, request, limits,
                () -> fake.nanos, () -> NOW);
    }

    private static void exactIdentityAndCalls() throws Exception {
        Fake fake = new Fake();
        var result = collect(fake);
        equal(JID, result.jidHex());
        equal(4, result.originals().size());
        equal(2, result.witnesses().size());
        String metadata = new String(result.metadata(), StandardCharsets.US_ASCII);
        check(metadata.contains("\"format_version\":\"quadringent-member-data-identity-v1\""), "versioned metadata");
        check(metadata.contains("\"admission\":\"not_evaluated\""), "no admission claim");
        check(metadata.contains(LARGE.toString()) && metadata.contains(MAX.toString()), "21-digit scope retained");
        check(result.metadata().length <= 65536, "metadata cap");
        check(!metadata.contains("CANARY_LOCAL_DETAIL"), "no error payload");
        check(fake.readOnly && fake.forward && fake.cursorReadOnly, "read-only JDBC");
        equal(2, fake.closedRows); equal(2, fake.closedStatements);
        check(!fake.connectionClosed, "borrowed connection remains open");
        equal(2, fake.sqls.size());
        for (String sql : fake.sqls) {
            check(sql.startsWith("SELECT HEX(JOURNAL_IDENTIFIER) AS JID_HEX"), "native JID bytes");
            check(sql.contains("OBJECT_LIBRARY => ?") && sql.contains("OBJECT_MEMBER => ?"), "positive current-member lookup");
            check(sql.contains("ENDING_RECEIVER_NAME => ?") && sql.contains("ENDING_SEQUENCE => ?"), "explicit finite window");
            check(sql.contains("EOF_DELAY => 0") && sql.contains("FETCH FIRST 1 ROW ONLY"), "positive witness only");
            check(sql.contains("JOURNAL_ENTRY_TYPE = 'JM'") && !sql.contains("ENTRY_DATA"), "existing metadata only");
            check(!sql.contains("INSERT") && !sql.contains("CALL ") && !sql.contains("OUTFILE"), "no DML or outfile");
        }
        for (Map<Integer, Object> params : fake.bindings) {
            equal(11, params.size());
            equal(new BigDecimal(LARGE), params.get(5)); equal(new BigDecimal(MAX), params.get(8));
            equal("TESTLIB", params.get(9)); equal("ITEMS", params.get(10)); equal("ITEMS", params.get(11));
        }
        equal(List.of("QUSRMBRD", "QJORJIDI", "QJORJIDI", "QUSRMBRD"),
                fake.calls.stream().map(ReadOnlyMemberIdentity.ProgramRequest::program).toList());
        for (var call : fake.calls) {
            check(call.receiverLength() > 0 && call.receiverLength() <= 4096, "bounded native output");
            List<byte[]> params = call.parameters();
            if (call.program().equals("QUSRMBRD")) {
                equal(8, params.size());
                equal("MBRD0200", text(params.get(2)));
                equal("ITEMS     TESTLIB   ", text(params.get(3)));
                equal("ITEMS     ", text(params.get(4)));
                equal("0", text(params.get(5))); equal("0", text(params.get(7)));
                equal(16, ByteBuffer.wrap(params.get(6)).getInt());
            } else {
                equal(6, params.size());
                equal("JRN       TESTLIB   ", text(params.get(2)));
                equal(JID, HexFormat.of().formatHex(params.get(3)));
                equal("RJID0100", text(params.get(4)));
                equal(94, call.receiverLength());
            }
        }
        check(fake.timeouts.stream().allMatch(value -> value > 0 && value <= 60), "SQL/native timeout bounds");
        var original = result.originals().getFirst();
        byte[] bytes = original.receiver(); bytes[8] ^= 1;
        check(!Arrays.equals(bytes, original.receiver()), "original bytes defensively retained");
        byte[] emitted = result.metadata(); emitted[0] = 0;
        check(result.metadata()[0] == '{', "metadata immutable");
        byte[] params = fake.calls.getFirst().parameters().get(4); params[0] = 0;
        check(fake.calls.getFirst().parameters().get(4)[0] != 0, "program request immutable");
        Fake unusedTail = new Fake(); unusedTail.fault = "unused_native_tail";
        var bounded = collect(unusedTail);
        equal(422, bounded.originals().getFirst().receiver().length);
        check(!new String(bounded.metadata(), StandardCharsets.US_ASCII).contains("cafebabefeedbeef"),
                "unreturned receiver bytes are never exported");
        Fake startOnly = new Fake(); startOnly.fault = "jm_witness";
        var fromStart = collect(startOnly).witnesses().getFirst();
        equal("F", fromStart.code()); equal("JM", fromStart.entryType());
    }

    private static void inputAndSequenceBounds() throws Exception {
        for (BigInteger[] pair : List.of(new BigInteger[]{BigInteger.ZERO, BigInteger.ZERO},
                new BigInteger[]{MAX.add(BigInteger.ONE), MAX},
                new BigInteger[]{BigInteger.ONE, MAX.add(BigInteger.ONE)},
                new BigInteger[]{BigInteger.TEN, BigInteger.valueOf(8)})) {
            invalid(() -> request(pair[0], pair[1]));
        }
        for (String name : List.of("*FIRST", "*LIBL", "ITEMS;DROP", "items", "A".repeat(11))) {
            invalid(() -> new ReadOnlyMemberIdentity.Request("TESTLIB", "JRN", "RCVLIB", "RCV9",
                    "TESTLIB", "ITEMS", name, LARGE, MAX, SHA));
        }
        invalid(() -> new ReadOnlyMemberIdentity.Limits(65537, 60));
        invalid(() -> new ReadOnlyMemberIdentity.Limits(0, 60));
        invalid(() -> new ReadOnlyMemberIdentity.Limits(65536, 61));
        invalid(() -> new ReadOnlyMemberIdentity.Limits(65536, 0));
        Fake empty = new Fake();
        failure(() -> collect(empty, request(BigInteger.ONE, BigInteger.ZERO), ReadOnlyMemberIdentity.Limits.defaults()),
                "unsupported_member_jid");
        equal(0, empty.calls.size()); equal(0, empty.sqls.size()); check(!empty.readOnly, "empty has no calls");
        Fake max = new Fake(); max.sequence = new BigDecimal(MAX);
        equal(MAX.toString(), collect(max).witnesses().getFirst().sequence());
        for (BigDecimal sequence : List.of(new BigDecimal("1.5"), new BigDecimal("-1"),
                new BigDecimal(MAX.add(BigInteger.ONE)), new BigDecimal("1E+999999999"))) {
            Fake fake = new Fake(); fake.sequence = sequence;
            failure(() -> collect(fake), "witness_invalid");
        }
    }

    private static void nativeFailures() throws Exception {
        for (String fault : List.of("native_false", "native_null", "error_available", "error_reserved",
                "error_code", "error_short", "error_provided", "rjid_short", "rjid_truncated", "rjid_returned",
                "rjid_journal", "rjid_journal_library", "rjid_library", "rjid_file", "rjid_member", "rjid_type_file", "rjid_type_index",
                "rjid_jid", "rjid_fid", "member_short", "member_truncated", "member_library", "member_file",
                "member_name", "member_logical", "member_remote", "member_changed", "member_extra_offset",
                "member_partial", "member_rollback", "member_date", "native_throw")) {
            Fake fake = new Fake(); fake.fault = fault;
            failure(() -> collect(fake), null);
        }
        Fake delayed = new Fake(); delayed.fault = "native_late";
        failure(() -> collect(delayed), "deadline_exceeded");
        Fake changed = new Fake(); changed.fault = "jid_changed";
        failure(() -> collect(changed), "identity_changed");
    }

    private static void jdbcFailures() throws Exception {
        for (String fault : List.of("no_rows", "extra_rows", "metadata_null", "metadata_extra", "metadata_wrong",
                "metadata_float", "metadata_precision", "metadata_result", "jid_bad", "jid_zero", "jid_null",
                "witness_receiver", "witness_library", "witness_object", "witness_type", "witness_code",
                "witness_entry", "sequence_null", "execute_throw", "read_throw", "rows_close", "statement_close")) {
            Fake fake = new Fake(); fake.fault = fault;
            failure(() -> collect(fake), null);
        }
        for (String fault : List.of("execute_late", "read_late", "close_late")) {
            Fake fake = new Fake(); fake.fault = fault;
            failure(() -> collect(fake), "deadline_exceeded");
        }
    }

    private static void budgetsAndOutput() throws Exception {
        Fake small = new Fake();
        failure(() -> collect(small, request(LARGE, MAX), new ReadOnlyMemberIdentity.Limits(100, 60)), "metadata_budget_exceeded");
        Fake reverse = new Fake(); reverse.fault = "clock_reverse";
        failure(() -> collect(reverse), "deadline_exceeded");
        Fake fake = new Fake();
        var result = collect(fake);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int status = cli(new Fake(), out, new ByteArrayOutputStream(), input());
        equal(0, status); equal(new String(result.metadata(), StandardCharsets.US_ASCII), out.toString(StandardCharsets.US_ASCII));
        for (String stage : List.of("write", "flush")) {
            OutputStream broken = new OutputStream() {
                @Override public void write(int value) throws IOException { if (stage.equals("write")) throw new IOException("CANARY_LOCAL_DETAIL"); }
                @Override public void flush() throws IOException { throw new IOException("CANARY_LOCAL_DETAIL"); }
            };
            ByteArrayOutputStream errors = new ByteArrayOutputStream();
            equal(2, cli(new Fake(), broken, errors, input()));
            check(!errors.toString(StandardCharsets.US_ASCII).contains("CANARY"), "output error sanitized");
        }
        ByteArrayOutputStream errors = new ByteArrayOutputStream();
        PrintStream swallowed = new PrintStream(new OutputStream() {
            @Override public void write(int value) throws IOException { throw new IOException("CANARY_LOCAL_DETAIL"); }
        });
        equal(2, cli(new Fake(), swallowed, errors, input()));
        Fake late = new Fake();
        OutputStream slow = new ByteArrayOutputStream() {
            @Override public void flush() { late.nanos = 61_000_000_000L; }
        };
        equal(2, cli(late, slow, errors, input()));
    }

    private static void cliAndTls() throws Exception {
        for (byte[] bad : List.of("host\nuser\n".getBytes(StandardCharsets.UTF_8),
                "host\r\nuser\nfixture\n".getBytes(StandardCharsets.UTF_8),
                new byte[]{(byte) 0xff, '\n'}, "bad host\nUSER\nfixture\n".getBytes(StandardCharsets.UTF_8),
                ("a".repeat(254) + "\nUSER\nfixture\n").getBytes(StandardCharsets.UTF_8),
                "host\nUSER\n\u0000\n".getBytes(StandardCharsets.UTF_8))) {
            Fake fake = new Fake(); ByteArrayOutputStream out = new ByteArrayOutputStream();
            equal(2, cli(fake, out, new ByteArrayOutputStream(), bad));
            check(!fake.opened && out.size() == 0, "bad credentials have no calls/output");
        }
        for (String property : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String prior = System.getProperty(property);
            try {
                System.setProperty(property, "true"); Fake fake = new Fake();
                equal(2, cli(fake, new ByteArrayOutputStream(), new ByteArrayOutputStream(), input()));
                check(!fake.opened, "TLS bypass refused before connection");
            } finally {
                if (prior == null) System.clearProperty(property); else System.setProperty(property, prior);
            }
        }
        Fake closed = new Fake(); closed.fault = "connection_close"; ByteArrayOutputStream out = new ByteArrayOutputStream();
        equal(2, cli(closed, out, new ByteArrayOutputStream(), input())); equal(0, out.size());
        Fake late = new Fake(); late.fault = "open_late";
        equal(2, cli(late, new ByteArrayOutputStream(), new ByteArrayOutputStream(), input()));
        check(late.sessionClosed, "late opened session closed");
        Fake throwing = new Fake(); throwing.fault = "open_throw"; ByteArrayOutputStream errors = new ByteArrayOutputStream();
        equal(2, cli(throwing, new ByteArrayOutputStream(), errors, input()));
        check(!errors.toString(StandardCharsets.US_ASCII).contains("CANARY"), "connection error sanitized");
        String[] bad = {"TESTLIB", "JRN", "RCVLIB", "RCV9", "TESTLIB", "ITEMS", "ITEMS",
                "1".repeat(10000), MAX.toString(), SHA, "65536", "60"};
        int[] connections = {0};
        equal(2, ReadOnlyMemberIdentity.run(bad, new ByteArrayInputStream(input()), new ByteArrayOutputStream(), errors,
                (host, user, secret, timeout) -> { connections[0]++; throw new AssertionError("unexpected connection"); }, () -> 0L, () -> NOW));
        equal(0, connections[0]);
    }

    private static byte[] input() { return "localhost\nTESTUSR\nfixture\n".getBytes(StandardCharsets.UTF_8); }

    private static int cli(Fake fake, OutputStream output, OutputStream errors, byte[] input) {
        String[] args = {"TESTLIB", "JRN", "RCVLIB", "RCV9", "TESTLIB", "ITEMS", "ITEMS",
                LARGE.toString(), MAX.toString(), SHA, "65536", "60"};
        return ReadOnlyMemberIdentity.run(args, new ByteArrayInputStream(input), output, errors,
                (host, user, secret, timeout) -> {
                    fake.opened = true;
                    if (fake.fault.equals("open_throw")) throw new SQLException("CANARY_LOCAL_DETAIL");
                    if (fake.fault.equals("open_late")) fake.nanos = 61_000_000_000L;
                    return new ReadOnlyMemberIdentity.Session() {
                        @Override public Connection connection() { return fake.connection(); }
                        @Override public ReadOnlyMemberIdentity.NativeProgram program() { return fake::nativeCall; }
                        @Override public int ccsid() { return CCSID; }
                        @Override public void close() throws SQLException {
                            fake.sessionClosed = true;
                            if (fake.fault.equals("connection_close")) throw new SQLException("CANARY_LOCAL_DETAIL");
                        }
                    };
                }, () -> fake.nanos, () -> NOW);
    }

    private static final class Fake {
        String fault = "";
        long nanos;
        boolean readOnly, forward, cursorReadOnly, connectionClosed, opened, sessionClosed;
        int closedRows, closedStatements;
        BigDecimal sequence = new BigDecimal(LARGE);
        final List<String> sqls = new ArrayList<>();
        final List<Map<Integer, Object>> bindings = new ArrayList<>();
        final List<Integer> timeouts = new ArrayList<>();
        final List<ReadOnlyMemberIdentity.ProgramRequest> calls = new ArrayList<>();

        Connection connection() {
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[]{Connection.class}, (p, m, a) -> {
                if (m.getName().equals("setReadOnly")) { readOnly = (boolean) a[0]; return null; }
                if (m.getName().equals("close")) { connectionClosed = true; return null; }
                if (m.getName().equals("prepareStatement")) {
                    sqls.add((String) a[0]); forward = (int) a[1] == ResultSet.TYPE_FORWARD_ONLY;
                    cursorReadOnly = (int) a[2] == ResultSet.CONCUR_READ_ONLY;
                    Map<Integer, Object> params = new HashMap<>(); bindings.add(params);
                    return statement(params, sqls.size());
                }
                return defaultValue(m.getReturnType());
            });
        }

        PreparedStatement statement(Map<Integer, Object> params, int ordinal) {
            return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(), new Class<?>[]{PreparedStatement.class}, (p, m, a) -> {
                switch (m.getName()) {
                    case "setString", "setBigDecimal" -> { params.put((int) a[0], a[1]); return null; }
                    case "setQueryTimeout" -> { timeouts.add((int) a[0]); return null; }
                    case "getMetaData" -> { return metadata(false); }
                    case "executeQuery" -> {
                        if (fault.equals("execute_throw")) throw new SQLException("CANARY_LOCAL_DETAIL");
                        if (fault.equals("execute_late")) nanos = 61_000_000_000L;
                        return rows(ordinal);
                    }
                    case "close" -> {
                        closedStatements++;
                        if (fault.equals("statement_close")) throw new SQLException("CANARY_LOCAL_DETAIL");
                        if (fault.equals("close_late")) nanos = 61_000_000_000L;
                        return null;
                    }
                    default -> { return defaultValue(m.getReturnType()); }
                }
            });
        }

        ResultSetMetaData metadata(boolean result) {
            if (fault.equals("metadata_null")) return null;
            String[] names = {"JID_HEX", "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT", "OBJECT_TYPE"};
            return (ResultSetMetaData) Proxy.newProxyInstance(ResultSetMetaData.class.getClassLoader(), new Class<?>[]{ResultSetMetaData.class}, (p, m, a) -> {
                int col = a == null || a.length == 0 ? 0 : (int) a[0];
                return switch (m.getName()) {
                    case "getColumnCount" -> fault.equals("metadata_extra") ? 9 : 8;
                    case "getColumnLabel" -> fault.equals("metadata_wrong") || (result && fault.equals("metadata_result")) ? "WRONG" : names[col - 1];
                    case "getColumnType" -> col == 4 ? (fault.equals("metadata_float") ? Types.DOUBLE : Types.DECIMAL) : Types.VARCHAR;
                    case "getPrecision" -> col == 4 ? (fault.equals("metadata_precision") ? 20 : 21) : new int[]{20,10,10,0,1,2,30,10}[col - 1];
                    case "getScale" -> 0;
                    default -> defaultValue(m.getReturnType());
                };
            });
        }

        ResultSet rows(int ordinal) {
            int[] row = {0};
            return (ResultSet) Proxy.newProxyInstance(ResultSet.class.getClassLoader(), new Class<?>[]{ResultSet.class}, (p, m, a) -> {
                switch (m.getName()) {
                    case "next" -> {
                        if (fault.equals("read_throw")) throw new SQLException("CANARY_LOCAL_DETAIL");
                        if (fault.equals("read_late")) nanos = 61_000_000_000L;
                        return ++row[0] <= (fault.equals("no_rows") ? 0 : fault.equals("extra_rows") ? 2 : 1);
                    }
                    case "getMetaData" -> { return metadata(true); }
                    case "getBigDecimal" -> { return fault.equals("sequence_null") ? null : sequence; }
                    case "getString" -> {
                        int col = (int) a[0];
                        String value = switch (col) {
                            case 1 -> ordinal == 2 && fault.equals("jid_changed") ? "010203040506070809ee" : JID;
                            case 2 -> "RCVLIB"; case 3 -> "RCV9";
                            case 5 -> fault.equals("jm_witness") ? "F" : "R";
                            case 6 -> fault.equals("jm_witness") ? "JM" : "PT";
                            case 7 -> "ITEMS     TESTLIB   ITEMS     "; case 8 -> "*FILE";
                            default -> throw new AssertionError("unexpected text column");
                        };
                        if ((col == 1 && fault.equals("jid_null"))) return null;
                        if (col == 1 && fault.equals("jid_bad")) return "x".repeat(20);
                        if (col == 1 && fault.equals("jid_zero")) return "0".repeat(20);
                        if ((col == 2 && fault.equals("witness_library")) || (col == 3 && fault.equals("witness_receiver"))
                                || (col == 7 && fault.equals("witness_object")) || (col == 8 && fault.equals("witness_type"))
                                || (col == 5 && fault.equals("witness_code")) || (col == 6 && fault.equals("witness_entry"))) return "WRONG";
                        return value;
                    }
                    case "close" -> { closedRows++; if (fault.equals("rows_close")) throw new SQLException("CANARY_LOCAL_DETAIL"); return null; }
                    default -> { return defaultValue(m.getReturnType()); }
                }
            });
        }

        ReadOnlyMemberIdentity.NativeReply nativeCall(ReadOnlyMemberIdentity.ProgramRequest request, int timeout) throws Exception {
            calls.add(request); timeouts.add(timeout);
            if (fault.equals("clock_reverse")) nanos = -1;
            if (fault.equals("native_late")) nanos = 61_000_000_000L;
            if (fault.equals("native_throw")) throw new IOException("CANARY_LOCAL_DETAIL");
            if (fault.equals("native_null")) return null;
            byte[] data = request.program().equals("QUSRMBRD") ? member() : resolved(request.parameters().get(3));
            byte[] error = new byte[16]; ByteBuffer.wrap(error).putInt(16);
            switch (fault) {
                case "error_available" -> ByteBuffer.wrap(error).putInt(4, 32);
                case "error_reserved" -> error[15] = 1;
                case "error_code" -> error[8] = 1;
                case "error_short" -> error = new byte[8];
                case "error_provided" -> ByteBuffer.wrap(error).putInt(8);
                default -> { }
            }
            return new ReadOnlyMemberIdentity.NativeReply(!fault.equals("native_false"), data, error);
        }

        byte[] member() {
            byte[] data = new byte[4096];
            ByteBuffer.wrap(data).putInt(422).putInt(422);
            put(data, 8, 10, "ITEMS"); put(data, 18, 10, "TESTLIB"); put(data, 28, 10, "ITEMS");
            put(data, 38, 10, "PF"); put(data, 58, 13, "1260101000000");
            put(data, 134, 1, "0"); put(data, 135, 1, "0"); put(data, 136, 1, "0");
            put(data, 186, 13, ""); ByteBuffer.wrap(data).putInt(244, 266).putInt(248, 156);
            put(data, 266 + 124, 1, "0"); put(data, 266 + 125, 1, "0");
            switch (fault) {
                case "member_short" -> { return new byte[100]; }
                case "member_truncated" -> ByteBuffer.wrap(data).putInt(4, 5000);
                case "member_library" -> put(data,18,10,"OTHER");
                case "member_file" -> put(data,8,10,"OTHER");
                case "member_name" -> put(data,28,10,"OTHER");
                case "member_logical" -> put(data,136,1,"1");
                case "member_remote" -> put(data,135,1,"1");
                case "member_changed" -> { if (calls.size() == 4) put(data,186,13,"1260101000001"); }
                case "member_extra_offset" -> ByteBuffer.wrap(data).putInt(244, Integer.MAX_VALUE);
                case "member_partial" -> put(data,266+125,1,"1");
                case "member_rollback" -> put(data,266+124,1,"1");
                case "member_date" -> put(data,58,13,"1261332000000");
                case "unused_native_tail" -> System.arraycopy(HexFormat.of().parseHex("cafebabefeedbeef"),0,data,1000,8);
                default -> { }
            }
            return data;
        }

        byte[] resolved(byte[] jid) {
            byte[] data = new byte[94]; ByteBuffer.wrap(data).putInt(94).putInt(94);
            put(data,8,10,"JRN"); put(data,18,10,"TESTLIB"); System.arraycopy(jid,0,data,28,10);
            put(data,38,10,"ITEMS"); put(data,48,10,"TESTLIB"); put(data,58,10,"ITEMS");
            put(data,68,10,"*QDDS"); put(data,78,16,"");
            switch (fault) {
                case "rjid_short" -> { return new byte[80]; }
                case "rjid_truncated" -> ByteBuffer.wrap(data).putInt(4,95);
                case "rjid_returned" -> ByteBuffer.wrap(data).putInt(93);
                case "rjid_journal" -> put(data,8,10,"OTHER");
                case "rjid_journal_library" -> put(data,18,10,"OTHER");
                case "rjid_library" -> put(data,48,10,"OTHER");
                case "rjid_file" -> put(data,38,10,"OTHER");
                case "rjid_member" -> put(data,58,10,"OTHER");
                case "rjid_type_file" -> put(data,68,10,"*FILE");
                case "rjid_type_index" -> put(data,68,10,"*QDDSI");
                case "rjid_jid" -> data[28] ^= 1;
                case "rjid_fid" -> data[78] ^= 1;
                default -> { }
            }
            return data;
        }
    }

    private static void put(byte[] data, int offset, int width, String value) {
        System.arraycopy(new AS400Text(width, CCSID).toBytes(value), 0, data, offset, width);
    }
    private static String text(byte[] bytes) { return (String) new AS400Text(bytes.length, CCSID).toObject(bytes); }
    private static Object defaultValue(Class<?> type) {
        if (type == boolean.class) return false;
        if (type == int.class) return 0;
        if (type == long.class) return 0L;
        return null;
    }
    @FunctionalInterface private interface Checked { void run() throws Exception; }
    private static void invalid(Checked action) throws Exception {
        try { action.run(); throw new AssertionError("expected invalid input"); }
        catch (IllegalArgumentException expected) { checks++; }
    }
    private static void failure(Checked action, String code) throws Exception {
        try { action.run(); throw new AssertionError("expected refusal"); }
        catch (ReadOnlyMemberIdentity.IdentityFailure expected) {
            checks++; if (code != null) equal(code, expected.code());
            check(!expected.toString().contains("CANARY"), "failure excludes raw details");
            check(expected.getCause() == null, "no raw cause retained");
        }
    }
    private static void check(boolean condition, String message) { checks++; if (!condition) throw new AssertionError(message); }
    private static void equal(Object expected, Object actual) { checks++; if (!expected.equals(actual)) throw new AssertionError("expected " + expected + ", got " + actual); }
}
