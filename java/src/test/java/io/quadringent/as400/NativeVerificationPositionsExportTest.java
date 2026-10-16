package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

/** Offline independent-position and framed-stream contracts; no source connection. */
public final class NativeVerificationPositionsExportTest {
    private static final String SHA = "a".repeat(64);
    private static final BigInteger LARGE = new BigInteger("9223372036854775808");
    private static final BigInteger MAX = new BigInteger("999999999999999999999");
    private static final Instant NOW = Instant.parse("2026-01-01T00:00:00.123456789Z");
    private static int checks;

    private NativeVerificationPositionsExportTest() { }

    public static void main(String[] args) throws Exception {
        if ((args.length == 1 || args.length == 2) && args[0].equals("--emit-fixture")) {
            var bound = window(LARGE, LARGE.add(BigInteger.ONE));
            if (args.length == 2) bound = new NativeVerificationPositionsExport.Window(bound.journalLibrary(), bound.journalName(),
                    bound.receiverLibrary(), bound.receiverName(), bound.objectLibrary(), bound.objectName(),
                    bound.start(), bound.end(), args[1]);
            export(new Fake(LARGE, LARGE, LARGE.add(BigInteger.ONE)), bound,
                    NativeVerificationPositionsExport.Limits.defaults(), System.out);
            return;
        }
        exactPositionsAndSql();
        inputBoundsAndEmptyWindows();
        failuresAndMetadata();
        budgetsAndDeadlines();
        cliAndTls();
        System.out.println("native_verification_positions_export=PASS checks=" + checks);
    }

    private static NativeVerificationPositionsExport.Window window(BigInteger start, BigInteger end) {
        return new NativeVerificationPositionsExport.Window("TESTLIB", "JRN", "RCVLIB", "RCV9",
                "TESTLIB", "ITEMS", start, end, SHA);
    }

    private static NativeVerificationPositionsExport.Summary export(Fake fake,
            NativeVerificationPositionsExport.Window window, NativeVerificationPositionsExport.Limits limits,
            OutputStream out) throws Exception {
        return NativeVerificationPositionsExport.export(fake.connection(), window, limits, out, () -> 0L, () -> NOW);
    }

    private static void exactPositionsAndSql() throws Exception {
        Fake fake = new Fake(LARGE, LARGE, LARGE.add(BigInteger.ONE));
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        var summary = export(fake, window(LARGE, LARGE.add(BigInteger.ONE)),
                NativeVerificationPositionsExport.Limits.defaults(), out);
        String[] lines = out.toString(StandardCharsets.UTF_8).split("\n");
        equal(5, lines.length);
        equal("{\"format_version\":\"quadringent-native-row-stream-v1\",\"kind\":\"header\","
                + "\"dataset\":\"positions\",\"scope_sha256\":\"" + SHA + "\",\"started_at\":\"" + NOW + "\"}", lines[0]);
        equal("{\"kind\":\"row\",\"row\":{\"JOURNAL_RECEIVER\":\"RCV9\",\"JOURNAL_SEQUENCE\":\"" + LARGE + "\"}}", lines[1]);
        equal(lines[1], lines[2]);
        check(lines[3].contains(LARGE.add(BigInteger.ONE).toString()), "inclusive final position");
        String digest = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest((lines[1] + "\n" + lines[2] + "\n" + lines[3] + "\n").getBytes(StandardCharsets.UTF_8)));
        equal("{\"kind\":\"end\",\"row_count\":3,\"rows_sha256\":\"" + digest + "\",\"completed_at\":\"" + NOW + "\"}", lines[4]);
        equal(3L, summary.rowCount());
        equal((long) out.size(), summary.byteCount());
        equal(digest, summary.rowsSha256());
        equal(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(out.toByteArray())), summary.sha256());
        check(fake.readOnly && fake.forwardOnly && fake.readOnlyCursor, "read-only forward cursor");
        check(fake.dataClosedBeforeCount && fake.countClosed && fake.statementsClosed == 2, "resources close before completion");
        check(fake.fetchSize == 1000 && fake.timeouts.stream().allMatch(i -> i > 0 && i <= 900), "query and fetch bounds");
        equal(2, fake.sqls.size());
        String sql = fake.sqls.getFirst();
        check(sql.startsWith("SELECT RECEIVER_LIBRARY, RECEIVER_NAME, SEQUENCE_NUMBER"), "native result columns");
        check(sql.contains("STARTING_RECEIVER_LIBRARY => ?") && sql.contains("ENDING_RECEIVER_LIBRARY => ?")
                && sql.contains("STARTING_SEQUENCE => ?") && sql.contains("ENDING_SEQUENCE => ?"), "explicit parameterized inclusive window");
        check(sql.contains("JOURNAL_CODES => 'R'") && sql.contains("'PT','PX','UB','UP','DL'"), "contract row operations");
        check(!sql.contains("OBJECT_LIBRARY =>") && !sql.contains("OBJECT_NAME =>")
                && !sql.contains("OBJECT_OBJTYPE =>") && !sql.contains("OBJECT_MEMBER =>"), "no current-JID object filter");
        check(sql.contains("LEFT(OBJECT,20)") && sql.contains("OBJECT_TYPE = '*FILE'"), "physical object predicate after retrieval");
        check(sql.endsWith("ORDER BY SEQUENCE_NUMBER") && !sql.contains("FETCH FIRST"), "no data truncation limit");
        check(fake.sqls.getLast().contains("FETCH FIRST 2000001 ROWS ONLY"), "transparent count overflow sentinel");
        for (Map<Integer, Object> binding : fake.bindings) {
            equal(10, binding.size());
            equal("TESTLIB", binding.get(1)); equal("JRN", binding.get(2));
            equal("RCVLIB", binding.get(3)); equal("RCV9", binding.get(4));
            equal(new BigDecimal(LARGE), binding.get(5));
            equal("RCVLIB", binding.get(6)); equal("RCV9", binding.get(7));
            equal(new BigDecimal(LARGE.add(BigInteger.ONE)), binding.get(8));
            equal("ITEMS", binding.get(9)); equal("TESTLIB", binding.get(10));
        }
        ByteArrayOutputStream maximum = new ByteArrayOutputStream();
        export(new Fake(MAX), window(MAX, MAX), NativeVerificationPositionsExport.Limits.defaults(), maximum);
        check(maximum.toString(StandardCharsets.UTF_8).contains(MAX.toString()), "21-digit maximum preserved");
        Fake exactExponent = new Fake(BigInteger.TEN); exactExponent.fault = "sequence-exponent";
        ByteArrayOutputStream exponentOut = new ByteArrayOutputStream();
        export(exactExponent, window(BigInteger.TEN, BigInteger.TEN),
                NativeVerificationPositionsExport.Limits.defaults(), exponentOut);
        check(exponentOut.toString(StandardCharsets.UTF_8).contains("\"JOURNAL_SEQUENCE\":\"10\""), "exact bounded exponent is canonicalized without floating point");
        Fake padded = new Fake(BigInteger.ONE); padded.fault = "padded-native-name";
        equal(1L, export(padded, window(BigInteger.ONE, BigInteger.ONE), NativeVerificationPositionsExport.Limits.defaults(),
                new ByteArrayOutputStream()).rowCount());
    }

    private static void inputBoundsAndEmptyWindows() throws Exception {
        rejects(() -> window(BigInteger.ZERO, BigInteger.ONE));
        rejects(() -> window(BigInteger.ONE, BigInteger.valueOf(-1)));
        rejects(() -> window(BigInteger.valueOf(4), BigInteger.ONE));
        rejects(() -> window(MAX.add(BigInteger.ONE), MAX));
        rejects(() -> window(BigInteger.ONE, MAX.add(BigInteger.ONE)));
        rejects(() -> new NativeVerificationPositionsExport.Window("TESTLIB", "JRN", "RCVLIB", "*CURRENT",
                "TESTLIB", "ITEMS", BigInteger.ONE, BigInteger.TEN, SHA));
        rejects(() -> new NativeVerificationPositionsExport.Window("TESTLIB", "JRN", "RCVLIB", "RCV9",
                "TESTLIB", "ITEMS;DROP", BigInteger.ONE, BigInteger.TEN, SHA));
        rejects(() -> new NativeVerificationPositionsExport.Window("TESTLIB", "JRN", "RCVLIB", "RCV9",
                "TESTLIB", "ITEMS", BigInteger.ONE, BigInteger.TEN, "wrong"));
        Fake empty = new Fake();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        equal(0L, export(empty, window(BigInteger.ONE, BigInteger.ZERO),
                NativeVerificationPositionsExport.Limits.defaults(), out).rowCount());
        check(empty.sqls.isEmpty() && out.toString(StandardCharsets.UTF_8).contains("\"row_count\":0"), "logical empty window makes no native existence claim");
        Fake maximumEmpty = new Fake();
        equal(0L, export(maximumEmpty, window(MAX, MAX.subtract(BigInteger.ONE)),
                NativeVerificationPositionsExport.Limits.defaults(), new ByteArrayOutputStream()).rowCount());
        check(maximumEmpty.sqls.isEmpty(), "valid maximum-bound empty window");
        rejects(() -> new NativeVerificationPositionsExport.Limits(20_000_001, 1000, 1024, 1));
        rejects(() -> new NativeVerificationPositionsExport.Limits(1, 16L * 1024 * 1024 * 1024 + 1, 1024, 1));
        rejects(() -> new NativeVerificationPositionsExport.Limits(1, 1000, 8 * 1024 * 1024 + 1, 1));
        rejects(() -> new NativeVerificationPositionsExport.Limits(1, 1000, 1024, 0));
    }

    private static void failuresAndMetadata() throws Exception {
        for (String fault : List.of("metadata-null", "metadata-name", "metadata-extra", "metadata-float",
                "metadata-scale", "metadata-precision", "row-metadata", "receiver-null", "receiver-other", "library-other",
                "sequence-null", "sequence-fraction", "sequence-large", "sequence-negative", "before-start", "after-end",
                "row-error", "data-execute", "data-close", "statement-close", "count-small", "count-large", "count-null", "count-extra",
                "count-missing", "count-fraction", "count-metadata", "count-execute", "count-close", "count-statement-close")) {
            Fake fake = new Fake(BigInteger.ONE, BigInteger.TWO); fake.fault = fault;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(fake, window(BigInteger.ONE, BigInteger.TWO), NativeVerificationPositionsExport.Limits.defaults(), out));
            noFooter(out);
            if (fault.startsWith("metadata")) equal(0, out.size());
        }
        Fake reversed = new Fake(BigInteger.TWO, BigInteger.ONE);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        fails(() -> export(reversed, window(BigInteger.ONE, BigInteger.TWO), NativeVerificationPositionsExport.Limits.defaults(), out));
        noFooter(out);
        Fake noRows = new Fake();
        equal(0L, export(noRows, window(BigInteger.ONE, BigInteger.TWO), NativeVerificationPositionsExport.Limits.defaults(), new ByteArrayOutputStream()).rowCount());
        Fake overflow = new Fake(BigInteger.ONE); overflow.fault = "count-large";
        ByteArrayOutputStream overflowOut = new ByteArrayOutputStream();
        fails(() -> export(overflow, window(BigInteger.ONE, BigInteger.ONE),
                new NativeVerificationPositionsExport.Limits(1, 10000, 1024, 900), overflowOut));
        noFooter(overflowOut);
        check(overflow.sqls.getLast().contains("FETCH FIRST 2 ROWS ONLY"), "COUNT budget sentinel is validated, not reported as success");
    }

    private static void budgetsAndDeadlines() throws Exception {
        for (var limit : List.of(new NativeVerificationPositionsExport.Limits(1, 10000, 1024, 900),
                new NativeVerificationPositionsExport.Limits(10, 250, 1024, 900),
                new NativeVerificationPositionsExport.Limits(10, 10000, 100, 900))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(new Fake(BigInteger.ONE, BigInteger.TWO), window(BigInteger.ONE, BigInteger.TWO), limit, out)); noFooter(out);
        }
        for (String point : List.of("read", "count", "close")) {
            Fake fake = new Fake(BigInteger.ONE); long[] clock = {0};
            Runnable advance = () -> clock[0] = 901_000_000_000L;
            if (point.equals("read")) fake.onNext = advance;
            if (point.equals("count")) fake.onCount = advance;
            if (point.equals("close")) fake.onClose = advance;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> NativeVerificationPositionsExport.export(fake.connection(), window(BigInteger.ONE, BigInteger.ONE),
                    NativeVerificationPositionsExport.Limits.defaults(), out, () -> clock[0], () -> NOW)); noFooter(out);
        }
        ByteArrayOutputStream wall = new ByteArrayOutputStream(); int[] reads = {0};
        fails(() -> NativeVerificationPositionsExport.export(new Fake(BigInteger.ONE).connection(), window(BigInteger.ONE, BigInteger.ONE),
                NativeVerificationPositionsExport.Limits.defaults(), wall, () -> 0L, () -> reads[0]++ == 0 ? NOW : NOW.minusNanos(1))); noFooter(wall);
        ByteArrayOutputStream reversed = new ByteArrayOutputStream(); long[] ticks = {5L};
        fails(() -> NativeVerificationPositionsExport.export(new Fake(BigInteger.ONE).connection(), window(BigInteger.ONE, BigInteger.ONE),
                NativeVerificationPositionsExport.Limits.defaults(), reversed, () -> ticks[0]--, () -> NOW)); noFooter(reversed);
        for (Instant invalid : List.of(Instant.parse("0000-01-01T00:00:00Z"), Instant.parse("+10000-01-01T00:00:00Z"))) {
            ByteArrayOutputStream invalidUtc = new ByteArrayOutputStream();
            fails(() -> NativeVerificationPositionsExport.export(new Fake().connection(), window(BigInteger.ONE, BigInteger.ZERO),
                    NativeVerificationPositionsExport.Limits.defaults(), invalidUtc, () -> 0L, () -> invalid));
            equal(0, invalidUtc.size());
        }
        fails(() -> export(new Fake(BigInteger.ONE), window(BigInteger.ONE, BigInteger.ONE), NativeVerificationPositionsExport.Limits.defaults(),
                new OutputStream() { public void write(int b) throws IOException { throw new IOException("SYNTHETIC_SECRET"); } }));
        ByteArrayOutputStream flushed = new ByteArrayOutputStream() {
            public void flush() throws IOException { throw new IOException("SYNTHETIC_SECRET"); }
        };
        fails(() -> export(new Fake(BigInteger.ONE), window(BigInteger.ONE, BigInteger.ONE), NativeVerificationPositionsExport.Limits.defaults(), flushed));
        check(flushed.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\""), "transport failure still requires unsuccessful process status");
        PrintStream broken = new PrintStream(new OutputStream() {
            public void write(int b) throws IOException { throw new IOException("SYNTHETIC_SECRET"); }
        }, false, StandardCharsets.UTF_8);
        fails(() -> export(new Fake(BigInteger.ONE), window(BigInteger.ONE, BigInteger.ONE), NativeVerificationPositionsExport.Limits.defaults(), broken));
    }

    private static void cliAndTls() throws Exception {
        String[] args = {"TESTLIB", "JRN", "RCVLIB", "RCV9", "TESTLIB", "ITEMS", "1", "2", SHA,
                         "100", "10000", "1024", "900"};
        ByteArrayOutputStream out = new ByteArrayOutputStream(), err = new ByteArrayOutputStream();
        equal(0, NativeVerificationPositionsExport.run(args, input("host\nUSER\npw\n"), out, err,
                (h,u,p) -> { equal("host",h); equal("USER",u); equal("pw",p); return new Fake(BigInteger.ONE, BigInteger.TWO).connection(); }));
        equal("", err.toString(StandardCharsets.UTF_8));
        err.reset();
        check(NativeVerificationPositionsExport.run(args, new ByteArrayInputStream(new byte[]{(byte)0xc3,0x28}),
                new ByteArrayOutputStream(),err,(h,u,p)->{throw new AssertionError("malformed UTF8 must not connect");}) != 0,"malformed UTF8 refused");
        equal("native_positions_export_error=invalid_input\n",err.toString(StandardCharsets.UTF_8));
        for (String credentials : List.of("bad/host\nUSER\npw\n", "host\nUSER\n\n", "host\nUSER\n", "host\r\nUSER\npw\n", "host\nUSER\np\0w\n",
                "host\nUSER\n" + "x".repeat(4097) + "\n")) {
            err.reset();
            check(NativeVerificationPositionsExport.run(args, input(credentials), new ByteArrayOutputStream(), err,
                    (h,u,p) -> { throw new AssertionError("must not authenticate"); }) != 0, "credentials refused before connection");
            equal("native_positions_export_error=invalid_input\n", err.toString(StandardCharsets.UTF_8));
        }
        for (String value : List.of(MAX.add(BigInteger.ONE).toString(), "-1", "1e2", "01", "+1")) {
            String[] invalid = args.clone(); invalid[6] = value; err.reset();
            check(NativeVerificationPositionsExport.run(invalid, input("host\nUSER\npw\n"), new ByteArrayOutputStream(), err,
                    (h,u,p) -> { throw new AssertionError("invalid bounds must not connect"); }) != 0, "invalid decimal21 CLI input");
        }
        for (String property : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String prior = System.getProperty(property);
            try {
                System.setProperty(property,"true"); err.reset();
                check(NativeVerificationPositionsExport.run(args,input("host\nUSER\npw\n"),new ByteArrayOutputStream(),err,
                        (h,u,p) -> { throw new AssertionError("TLS bypass must not connect"); }) != 0,"trust-all refused");
                equal("native_positions_export_error=tls_required\n",err.toString(StandardCharsets.UTF_8));
            } finally { if(prior == null)System.clearProperty(property);else System.setProperty(property,prior); }
        }
        err.reset();
        check(NativeVerificationPositionsExport.run(args,input("host\nUSER\npw\n"),new ByteArrayOutputStream(),err,
                (h,u,p) -> { throw new SQLException("SYNTHETIC_SECRET endpoint credentials"); }) != 0,"connection failure nonzero");
        equal("native_positions_export_error=source_sql\n",err.toString(StandardCharsets.UTF_8));
        Fake close = new Fake(BigInteger.ONE); close.fault = "connection-close"; err.reset(); out.reset();
        check(NativeVerificationPositionsExport.run(args,input("host\nUSER\npw\n"),out,err,(h,u,p)->close.connection()) != 0,
                "close after footer cannot return zero");
        check(out.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\""),"completion requires exit zero and EOF");
    }

    private static ByteArrayInputStream input(String value) { return new ByteArrayInputStream(value.getBytes(StandardCharsets.UTF_8)); }
    private interface Action { void run() throws Exception; }
    private static void rejects(Action action) throws Exception {
        try { action.run(); throw new AssertionError("invalid input admitted"); } catch (IllegalArgumentException expected) { checks++; }
    }
    private static void fails(Action action) throws Exception {
        try { action.run(); throw new AssertionError("failed export admitted"); }
        catch (NativeVerificationPositionsExport.ExportFailure expected) {
            check(!expected.toString().contains("SYNTHETIC_SECRET") && expected.getCause() == null,"sanitized error");
        }
    }
    private static void noFooter(ByteArrayOutputStream out) { check(!out.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\""),"failure has no footer"); }
    private static void equal(Object expected,Object actual) { check(java.util.Objects.equals(expected,actual),"expected contract value"); }
    private static void check(boolean condition,String message) { if(!condition)throw new AssertionError(message);checks++; }

    private static final class Fake {
        final List<BigInteger> sequences;
        final List<String> sqls = new ArrayList<>();
        final List<Map<Integer,Object>> bindings = new ArrayList<>();
        final List<Integer> timeouts = new ArrayList<>();
        String fault = "";
        boolean readOnly,forwardOnly,readOnlyCursor,dataClosed,dataClosedBeforeCount,countClosed;
        int fetchSize,statementsClosed;
        Runnable onNext=()->{},onCount=()->{},onClose=()->{};
        Fake(BigInteger... values) { sequences=List.of(values); }
        Connection connection() {
            return proxy(Connection.class,(p,m,a)->switch(m.getName()) {
                case "setReadOnly" -> { readOnly=(boolean)a[0];yield null; }
                case "prepareStatement" -> {
                    check(readOnly,"read-only before SQL");
                    String sql=(String)a[0];sqls.add(sql);
                    forwardOnly=a.length==3 && (int)a[1]==ResultSet.TYPE_FORWARD_ONLY;
                    readOnlyCursor=a.length==3 && (int)a[2]==ResultSet.CONCUR_READ_ONLY;
                    boolean count=sql.startsWith("SELECT COUNT(*)");
                    if(count){dataClosedBeforeCount=dataClosed;onCount.run();}
                    Map<Integer,Object> bound=new HashMap<>();bindings.add(bound);
                    yield statement(count,bound);
                }
                case "close" -> { if(fault.equals("connection-close"))throw new SQLException("SYNTHETIC_SECRET");yield null; }
                default -> throw new AssertionError(m.getName());
            });
        }
        PreparedStatement statement(boolean count,Map<Integer,Object> bound) {
            return proxy(PreparedStatement.class,(p,m,a)->switch(m.getName()) {
                case "setString","setBigDecimal" -> { bound.put((int)a[0],a[1]);yield null; }
                case "setQueryTimeout" -> { timeouts.add((int)a[0]);yield null; }
                case "setFetchSize" -> { fetchSize=(int)a[0];yield null; }
                case "getMetaData" -> fault.equals("metadata-null") ? null : metadata(count,false);
                case "executeQuery" -> { if(fault.equals(count?"count-execute":"data-execute"))throw new SQLException("SYNTHETIC_SECRET");yield result(count); }
                case "close" -> { statementsClosed++;if(fault.equals(count?"count-statement-close":"statement-close"))throw new SQLException("SYNTHETIC_SECRET");yield null; }
                default -> throw new AssertionError("unexpected method: "+m.getName());
            });
        }
        ResultSet result(boolean count) {
            int[] index={-1};
            return proxy(ResultSet.class,(p,m,a)->switch(m.getName()) {
                case "next" -> {
                    if(!count){onNext.run();if(fault.equals("row-error"))throw new SQLException("SYNTHETIC_SECRET");}
                    yield ++index[0] < (count ? fault.equals("count-missing") ? 0 : fault.equals("count-extra") ? 2 : 1 : sequences.size());
                }
                case "getMetaData" -> metadata(count,true);
                case "getString" -> {
                    String value=(int)a[0]==1 ? fault.equals("library-other") ? "OTHER" : "RCVLIB" : fault.equals("receiver-other") ? "OTHER" : "RCV9";
                    if(fault.equals("padded-native-name"))value=String.format(java.util.Locale.ROOT,"%-10s",value);
                    yield fault.equals("receiver-null") ? null : value;
                }
                case "getBigDecimal" -> {
                    if(count)yield fault.equals("count-null") ? null : fault.equals("count-fraction") ? new BigDecimal("1.5") :
                            BigDecimal.valueOf(sequences.size()+(fault.equals("count-small")?-1:fault.equals("count-large")?1:0));
                    yield switch(fault) {
                        case "sequence-null" -> null;
                        case "sequence-fraction" -> new BigDecimal("1.5");
                        case "sequence-exponent" -> new BigDecimal("1E+1");
                        case "sequence-large" -> new BigDecimal(MAX.add(BigInteger.ONE));
                        case "sequence-negative" -> BigDecimal.valueOf(-1);
                        case "before-start" -> BigDecimal.ZERO;
                        case "after-end" -> BigDecimal.TEN;
                        default -> new BigDecimal(sequences.get(index[0]));
                    };
                }
                case "close" -> {
                    if(count){countClosed=true;onClose.run();}else dataClosed=true;
                    if(fault.equals(count?"count-close":"data-close"))throw new SQLException("SYNTHETIC_SECRET");yield null;
                }
                default -> throw new AssertionError("unexpected or lossy getter: "+m.getName());
            });
        }
        ResultSetMetaData metadata(boolean count,boolean result) {
            return proxy(ResultSetMetaData.class,(p,m,a)->switch(m.getName()) {
                case "getColumnCount" -> fault.equals("metadata-extra") ? 4 : count ? 1 : 3;
                case "getColumnLabel" -> fault.equals("metadata-name") || result && fault.equals("row-metadata") ? "OTHER" :
                        count ? "POSITION_COUNT" : switch((int)a[0]) {case 1->"RECEIVER_LIBRARY";case 2->"RECEIVER_NAME";default->"SEQUENCE_NUMBER";};
                case "getColumnType" -> fault.equals("metadata-float") || count && fault.equals("count-metadata") ? Types.DOUBLE :
                        count ? Types.BIGINT : (int)a[0]==3 ? Types.DECIMAL : Types.VARCHAR;
                case "getPrecision" -> count ? 19 : (int)a[0]==3 ? fault.equals("metadata-precision") ? 22 : 21 : 10;
                case "getScale" -> fault.equals("metadata-scale") ? 1 : 0;
                default -> throw new AssertionError(m.getName());
            });
        }
    }
    private static <T>T proxy(Class<T> type,java.lang.reflect.InvocationHandler handler) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(),new Class<?>[]{type},handler));
    }
}
