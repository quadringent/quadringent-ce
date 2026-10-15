package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.Reader;
import java.io.StringReader;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
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
import java.util.HexFormat;
import java.util.List;
import java.util.Properties;
import java.util.function.IntFunction;

/** Offline JDBC and wire-protocol contracts; no driver or source is contacted. */
public final class NativeVerificationSourceExportTest {
    private static final String SHA = "a".repeat(64);
    private static final Instant NOW = Instant.parse("2026-01-01T00:00:00Z");
    private static int checks;

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && args[0].equals("--bounds-only")) {
            serverBoundsPrecedeDriverMaterialization();
            System.out.println("native_source_server_bounds=PASS checks=" + checks);
            return;
        }
        if (args.length == 1 && args[0].equals("--emit-fixture")) {
            Fake fake = new Fake(new String[]{"ID", "LABEL", "AMOUNT", "STAMP"},
                    new int[]{Types.BIGINT, Types.CHAR, Types.DECIMAL, Types.TIMESTAMP}, 1,
                    i -> new Object[]{1L, "Café 日本語  ", new BigDecimal("12345678901234567890.123456789012345678"),
                                      "2026-01-01 00:00:00." + "123456" + "789012"});
            export(fake, scope(fake.names), NativeVerificationSourceExport.Limits.defaults(), System.out);
            return;
        }
        exactProtocolAndTypes();
        serverBoundsPrecedeDriverMaterialization();
        inputAndSqlGuards();
        failedCountsAndJournals();
        unsupportedTypes();
        boundedTextAndEncoding();
        budgetsAndFailures();
        cliPrivacyAndTls();
        if (args.length == 0 || !args[0].equals("--skip-million")) millionRowsWithoutPopulationBuffer();
        System.out.println("native_verification_source_export=PASS checks=" + checks);
    }

    private static void serverBoundsPrecedeDriverMaterialization() throws Exception {
        for (int type : new int[]{Types.CLOB, Types.NCLOB, Types.LONGVARCHAR, Types.LONGNVARCHAR}) {
            for (boolean locator : new boolean[]{false, true}) {
                Fake oversized = new Fake(new String[]{"ID", "LABEL"}, new int[]{Types.INTEGER, type}, 1,
                        i -> new Object[]{1, "x".repeat(17)});
                oversized.enforceServerBound = true;
                oversized.locator = locator;
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                fails(() -> export(oversized, scope(oversized.names), limits(2, 10000, 1024, 4), out));
                noFooter(out);
                equal(0, oversized.driverMaterializedChars);
                check(oversized.executedSql.contains("CASE WHEN"), "guard before real data execution");
                check(oversized.executedSql.contains("OCTET_LENGTH("), "unambiguous SQL octet ceiling");
                check(!oversized.executedSql.contains("SUBSTR"), "no silent substring truncation");
            }
        }
        Fake wide = new Fake(new String[]{"ID", "FIRST", "SECOND"},
                new int[]{Types.INTEGER, Types.CLOB, Types.NCLOB}, 1,
                i -> new Object[]{1, "x".repeat(600), "y".repeat(600)});
        wide.enforceServerBound = true;
        ByteArrayOutputStream wideOut = new ByteArrayOutputStream();
        fails(() -> export(wide, scope(wide.names), limits(2, 10000, 256, 1000), wideOut));
        noFooter(wideOut);
        equal(0, wide.driverMaterializedChars);
        for (String fault : List.of("flag-null", "flag-one", "flag-two", "no-metadata")) {
            Fake invalid = basic(1);
            invalid.fault = fault;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(invalid, scope(invalid.names), NativeVerificationSourceExport.Limits.defaults(), out));
            noFooter(out);
            if (fault.equals("no-metadata")) equal("", invalid.executedSql);
        }
        Fake exact = new Fake(new String[]{"ID", "LABEL"}, new int[]{Types.INTEGER, Types.NCLOB}, 1,
                i -> new Object[]{1, "😀  "});
        exact.enforceServerBound = true;
        ByteArrayOutputStream exactOut = new ByteArrayOutputStream();
        export(exact, scope(exact.names), limits(2, 10000, 1024, 4), exactOut);
        check(exactOut.toString(StandardCharsets.UTF_8).contains("\"LABEL\":\"😀  \""), "exact character cap preserves Unicode and CHAR spaces");
        equal(4, exact.driverMaterializedChars);
        Fake applicationCap = new Fake(new String[]{"ID", "LABEL"}, new int[]{Types.INTEGER, Types.CLOB}, 1,
                i -> new Object[]{1, "x".repeat(16)});
        applicationCap.enforceServerBound = true;
        ByteArrayOutputStream appOut = new ByteArrayOutputStream();
        fails(() -> export(applicationCap, scope(applicationCap.names), limits(2, 10000, 1024, 4), appOut));
        noFooter(appOut);
        equal(16, applicationCap.driverMaterializedChars);
        Fake collisions = new Fake(new String[]{"ID", "QDT_SOURCE_SAFE_0", "QDT_SOURCE_OVERSIZE_0"},
                new int[]{Types.INTEGER, Types.CHAR, Types.NCLOB}, 1, i -> new Object[]{1, "  ", ""});
        ByteArrayOutputStream collisionOut = new ByteArrayOutputStream();
        export(collisions, scope(collisions.names), NativeVerificationSourceExport.Limits.defaults(), collisionOut);
        check(collisions.executedSql.contains(" AS QDT_SOURCE_SAFE_1"), "safe alias cannot shadow business column");
        check(collisions.executedSql.contains(" AS QDT_SOURCE_OVERSIZE_1"), "flag alias cannot shadow business column");
        check(collisionOut.toString(StandardCharsets.UTF_8).contains("\"QDT_SOURCE_SAFE_0\":\"  \","
                + "\"QDT_SOURCE_OVERSIZE_0\":\"\""), "business names and spaces preserved");
    }

    private static NativeVerificationSourceExport.Scope scope(String... columns) {
        return new NativeVerificationSourceExport.Scope("TESTLIB", "ITEMS", "TESTLIB", "JRN",
                List.of("ID"), List.of(columns), SHA);
    }

    private static NativeVerificationSourceExport.Limits limits(long rows, long bytes, int line, int cell) {
        return new NativeVerificationSourceExport.Limits(rows, bytes, line, cell, 900);
    }

    private static NativeVerificationSourceExport.Summary export(Fake fake,
            NativeVerificationSourceExport.Scope scope, NativeVerificationSourceExport.Limits limits,
            OutputStream out) throws Exception {
        return NativeVerificationSourceExport.export(fake.connection(), scope, limits, out, () -> 0L, () -> NOW);
    }

    private static void exactProtocolAndTypes() throws Exception {
        Fake fake = new Fake(new String[]{"ID", "AMOUNT", "LABEL", "EMPTY", "MISSING", "DAY", "STAMP"},
                new int[]{Types.BIGINT, Types.DECIMAL, Types.CHAR, Types.VARCHAR, Types.CLOB,
                          Types.DATE, Types.TIMESTAMP}, 1,
                i -> new Object[]{7L, new BigDecimal("12345678901234567890123456789012345678.00"),
                                  "Café 日本語  \n\"\\", "", null, java.sql.Date.valueOf("2026-01-02"),
                                  "2026-01-02 03:04:05." + "123456" + "789012"});
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        var summary = export(fake, scope(fake.names), NativeVerificationSourceExport.Limits.defaults(), out);
        String[] lines = out.toString(StandardCharsets.UTF_8).split("\n");
        equal(3, lines.length);
        equal("{\"format_version\":\"quadringent-native-row-stream-v1\",\"kind\":\"header\","
                + "\"dataset\":\"source\",\"scope_sha256\":\"" + SHA
                + "\",\"started_at\":\"2026-01-01T00:00:00Z\"}", lines[0]);
        equal("{\"kind\":\"row\",\"row\":{\"ID\":\"7\","
                + "\"AMOUNT\":\"12345678901234567890123456789012345678.00\","
                + "\"LABEL\":\"Café 日本語  \\n\\\"\\\\\",\"EMPTY\":\"\",\"MISSING\":null,"
                + "\"DAY\":\"2026-01-02\",\"STAMP\":\"2026-01-02T03:04:05." + "123456" + "789012\"}}", lines[1]);
        String digest = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest((lines[1] + "\n").getBytes(StandardCharsets.UTF_8)));
        equal("{\"kind\":\"end\",\"row_count\":1,\"rows_sha256\":\"" + digest
                + "\",\"completed_at\":\"2026-01-01T00:00:00Z\"}", lines[2]);
        equal(1L, summary.rowCount());
        equal((long) out.size(), summary.byteCount());
        equal(digest, summary.rowsSha256());
        check(fake.readOnly && fake.forwardOnly && fake.readOnlyCursor, "read-only forward cursor");
        check(fake.fetchSize == 1000 && fake.timeouts.stream().allMatch(x -> x == 900), "JDBC bounds");
        check(fake.rowCursorClosedBeforeCount && fake.countClosed, "close reads before footer");
        check(fake.executedSql.endsWith(" ORDER BY ID"), "primary-key ordering");
        equal("SELECT COUNT(*) FROM TESTLIB.ITEMS", fake.sqls.get(fake.sqls.size() - 1));
        check(fake.textReads == 4, "bounded reader used for text and exact timestamp cells");
        check(!out.toString(StandardCharsets.UTF_8).contains("stable_source"), "no source-cut claim");
        Fake nulls = new Fake(new String[]{"ID", "AMOUNT", "DAY", "STAMP"},
                new int[]{Types.INTEGER, Types.DECIMAL, Types.DATE, Types.TIMESTAMP}, 1,
                i -> new Object[]{1, null, null, null});
        ByteArrayOutputStream nullOut = new ByteArrayOutputStream();
        export(nulls, scope(nulls.names), NativeVerificationSourceExport.Limits.defaults(), nullOut);
        check(nullOut.toString(StandardCharsets.UTF_8).contains("\"AMOUNT\":null,\"DAY\":null,\"STAMP\":null"),
                "numeric and temporal NULLs preserved");
        Fake empty = new Fake(new String[]{"ID"}, new int[]{Types.INTEGER}, 0, i -> new Object[]{i});
        ByteArrayOutputStream zero = new ByteArrayOutputStream();
        export(empty, scope("ID"), NativeVerificationSourceExport.Limits.defaults(), zero);
        check(zero.toString(StandardCharsets.UTF_8).contains("\"row_count\":0"), "empty source is complete zero");
    }

    private static void inputAndSqlGuards() throws Exception {
        rejects(() -> scope("ID", "id"));
        rejects(() -> scope("LABEL"));
        rejects(() -> scope("ID;DROP"));
        rejects(() -> new NativeVerificationSourceExport.Scope("TESTLIB", "ITEMS", "TESTLIB", "JRN",
                List.of("ID", "id"), List.of("ID"), SHA));
        rejects(() -> new NativeVerificationSourceExport.Scope("TESTLIB", "ITEMS", "TESTLIB", "JRN",
                List.of("ID"), List.of("ID"), "not-sha"));
        rejects(() -> limits(20_000_001, 1024, 1024, 100));
        rejects(() -> limits(1, 16L * 1024 * 1024 * 1024 + 1, 1024, 100));
        rejects(() -> limits(1, 1024, 8 * 1024 * 1024 + 1, 100));
        Fake fake = new Fake(new String[]{"A", "B"}, new int[]{Types.INTEGER, Types.INTEGER}, 1,
                i -> new Object[]{1, 2});
        var composite = new NativeVerificationSourceExport.Scope("TESTLIB", "ITEMS", "TESTLIB", "JRN",
                List.of("B", "A"), List.of("A", "B"), SHA);
        export(fake, composite, NativeVerificationSourceExport.Limits.defaults(), new ByteArrayOutputStream());
        equal("SELECT A,B FROM TESTLIB.ITEMS ORDER BY B,A", fake.executedSql);
        check(fake.sqls.stream().allMatch(sql -> sql.startsWith("SELECT ")), "SELECT only");
    }

    private static void failedCountsAndJournals() throws Exception {
        for (String fault : List.of("count-small", "count-large", "count-null", "count-extra", "journal-missing",
                "journal-other", "journal-extra", "metadata-name", "close-error")) {
            Fake fake = basic(2);
            fake.fault = fault;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(fake, scope(fake.names), NativeVerificationSourceExport.Limits.defaults(), out));
            noFooter(out);
            if (fault.startsWith("journal")) equal(0, out.size());
        }
    }

    private static void unsupportedTypes() throws Exception {
        for (int type : new int[]{Types.FLOAT, Types.REAL, Types.DOUBLE, Types.BINARY, Types.VARBINARY,
                Types.BLOB, Types.ARRAY, Types.JAVA_OBJECT, Types.BOOLEAN, Types.TIME, Types.TIMESTAMP_WITH_TIMEZONE}) {
            Fake fake = new Fake(new String[]{"ID"}, new int[]{type}, 1, i -> new Object[]{1});
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(fake, scope("ID"), NativeVerificationSourceExport.Limits.defaults(), out));
            equal(0, out.size());
        }
        for (String stamp : List.of("2026-01-02 00:00:00.1234567890123", "2026-02-30 00:00:00",
                "2026-01-02 25:00:00", "2026-01-02 00:00:00+01:00")) {
            Fake precision = new Fake(new String[]{"ID", "STAMP"}, new int[]{Types.INTEGER, Types.TIMESTAMP}, 1,
                    i -> new Object[]{1, stamp});
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(precision, scope(precision.names), NativeVerificationSourceExport.Limits.defaults(), out));
            noFooter(out);
        }
    }

    private static void boundedTextAndEncoding() throws Exception {
        for (String value : List.of("bad\ud800", "bad\udc00", "\ud800x")) {
            Fake fake = new Fake(new String[]{"ID", "LABEL"}, new int[]{Types.INTEGER, Types.CLOB}, 1,
                    i -> new Object[]{1, value});
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(fake, scope(fake.names), NativeVerificationSourceExport.Limits.defaults(), out));
            noFooter(out);
            check(!out.toString(StandardCharsets.UTF_8).contains("\"kind\":\"row\""), "no lossy row");
        }
        Fake huge = basic(1);
        huge.endlessText = true;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        fails(() -> export(huge, scope(huge.names), limits(2, 10000, 1024, 32), out));
        noFooter(out);
        check(huge.charactersRead <= 33, "LOB read stops at cell bound plus sentinel");
        Fake validPair = new Fake(new String[]{"ID", "LABEL"}, new int[]{Types.INTEGER, Types.CLOB}, 1,
                i -> new Object[]{1, "emoji \ud83d\ude00  "});
        ByteArrayOutputStream pair = new ByteArrayOutputStream();
        export(validPair, scope(validPair.names), NativeVerificationSourceExport.Limits.defaults(), pair);
        check(pair.toString(StandardCharsets.UTF_8).contains("emoji 😀  "), "surrogate pair preserved");
    }

    private static void budgetsAndFailures() throws Exception {
        for (var limit : List.of(limits(1, 10000, 1024, 100), limits(10, 250, 1024, 100),
                limits(10, 10000, 200, 100))) {
            Fake fake = basic(2);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            fails(() -> export(fake, scope(fake.names), limit, out));
            noFooter(out);
        }
        Fake fake = basic(1);
        long[] clock = {0L};
        fake.onNext = () -> clock[0] = 901_000_000_000L;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        fails(() -> NativeVerificationSourceExport.export(fake.connection(), scope(fake.names),
                NativeVerificationSourceExport.Limits.defaults(), out, () -> clock[0], () -> NOW));
        noFooter(out);
        Fake finalRead = basic(1);
        long[] finalClock = {0L};
        finalRead.onCount = () -> finalClock[0] = 901_000_000_000L;
        ByteArrayOutputStream finalOut = new ByteArrayOutputStream();
        fails(() -> NativeVerificationSourceExport.export(finalRead.connection(), scope(finalRead.names),
                NativeVerificationSourceExport.Limits.defaults(), finalOut, () -> finalClock[0], () -> NOW));
        noFooter(finalOut);
        int[] wallReads = {0};
        ByteArrayOutputStream wallOut = new ByteArrayOutputStream();
        fails(() -> NativeVerificationSourceExport.export(basic(1).connection(), scope("ID", "LABEL"),
                NativeVerificationSourceExport.Limits.defaults(), wallOut, () -> 0L,
                () -> wallReads[0]++ == 0 ? NOW : NOW.minusSeconds(1)));
        noFooter(wallOut);
        Fake failed = basic(2);
        failed.fault = "row-error";
        ByteArrayOutputStream partial = new ByteArrayOutputStream();
        fails(() -> export(failed, scope(failed.names), NativeVerificationSourceExport.Limits.defaults(), partial));
        noFooter(partial);
        fails(() -> export(basic(1), scope("ID", "LABEL"), NativeVerificationSourceExport.Limits.defaults(),
                new OutputStream() { public void write(int b) throws IOException { throw new IOException("secret"); } }));
    }

    private static void cliPrivacyAndTls() throws Exception {
        String[] args = {"TESTLIB", "ITEMS", "TESTLIB", "JRN", "ID", "ID,LABEL", SHA, "10", "10000"};
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = NativeVerificationSourceExport.run(args, input("db.example.invalid\nSYNTHETIC\ncanary-password\n"),
                out, err, (host, user, password) -> {
                    equal("db.example.invalid", host); equal("SYNTHETIC", user); equal("canary-password", password);
                    throw new SQLException("jdbc:as400://private secret SQL canary-password", "28000");
                });
        check(code != 0, "failed login nonzero");
        equal("native_source_export_error=source_sql\n", err.toString(StandardCharsets.UTF_8));
        equal(0, out.size());
        Properties properties = NativeVerificationSourceExport.connectionProperties("SYNTHETIC", "canary-password");
        equal("true", properties.getProperty("secure"));
        equal("read only", properties.getProperty("access"));
        equal("false", properties.getProperty("prompt"));
        equal("iso", properties.getProperty("date format"));
        equal("120000", properties.getProperty("socket timeout"));
        out.reset(); err.reset();
        check(NativeVerificationSourceExport.run(args, input("host\nUSER\npw\n"), out, err,
                (h, u, p) -> basic(1).connection()) == 0, "CLI success");
        equal("", err.toString(StandardCharsets.UTF_8));
        check(out.toString(StandardCharsets.UTF_8).startsWith("{\"format_version\":"), "stdout only frames");
        check(out.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\""), "CLI complete footer");
        for (String credential : List.of("host/unsafe\nUSER\npw\n", "host\nUSER\n\n",
                "host\nUSER\n" + "p".repeat(4097) + "\n", "host\nUSER\n")) {
            err.reset();
            int rejected = NativeVerificationSourceExport.run(args, input(credential), new ByteArrayOutputStream(), err,
                    (h, u, p) -> { throw new AssertionError("invalid stdin must not connect"); });
            check(rejected != 0, "invalid stdin rejected");
            equal("native_source_export_error=invalid_input\n", err.toString(StandardCharsets.UTF_8));
        }
        for (String property : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String prior = System.getProperty(property);
            try {
                System.setProperty(property, "true");
                err.reset();
                check(NativeVerificationSourceExport.run(args, input("host\nUSER\npw\n"), out, err,
                        (h, u, p) -> { throw new AssertionError("trust-all must not connect"); }) != 0,
                        "trust-all rejected");
                equal("native_source_export_error=tls_required\n", err.toString(StandardCharsets.UTF_8));
            } finally {
                if (prior == null) System.clearProperty(property); else System.setProperty(property, prior);
            }
        }
    }

    private static void millionRowsWithoutPopulationBuffer() throws Exception {
        Fake fake = basic(1_000_000);
        Sink sink = new Sink();
        var summary = export(fake, scope(fake.names), NativeVerificationSourceExport.Limits.defaults(), sink);
        equal(1_000_000L, summary.rowCount());
        equal(sink.bytes, summary.byteCount());
        equal(1_000_000L, sink.rows);
        check(sink.footer.contains("\"row_count\":1000000"), "million complete footer");
        equal(HexFormat.of().formatHex(sink.digest.digest()), summary.rowsSha256());
        check(sink.maxWrite < 1024, "only one small frame buffered per million-row test");
    }

    private static Fake basic(int count) {
        return new Fake(new String[]{"ID", "LABEL"}, new int[]{Types.BIGINT, Types.VARCHAR}, count,
                i -> new Object[]{(long) i, "value  "});
    }

    private static ByteArrayInputStream input(String value) {
        return new ByteArrayInputStream(value.getBytes(StandardCharsets.UTF_8));
    }

    private static void noFooter(ByteArrayOutputStream out) {
        check(!out.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\""), "failure has no complete footer");
    }

    private interface Action { void run() throws Exception; }
    private static void rejects(Action action) throws Exception {
        try { action.run(); throw new AssertionError("invalid input accepted"); }
        catch (IllegalArgumentException expected) { checks++; }
    }
    private static void fails(Action action) throws Exception {
        try { action.run(); throw new AssertionError("failed export accepted"); }
        catch (NativeVerificationSourceExport.ExportFailure expected) {
            check(!expected.toString().contains("secret"), "failure sanitized");
        }
    }
    private static void equal(Object expected, Object actual) {
        check(java.util.Objects.equals(expected, actual), "expected " + expected + " got " + actual);
    }
    private static void check(boolean value, String name) {
        if (!value) throw new AssertionError(name);
        checks++;
    }

    private static final class Sink extends OutputStream {
        long bytes, rows;
        int maxWrite;
        String footer = "";
        final MessageDigest digest;
        Sink() throws Exception { digest = MessageDigest.getInstance("SHA-256"); }
        public void write(int b) { throw new AssertionError("expected whole bounded frame"); }
        public void write(byte[] data, int offset, int length) {
            bytes += length; maxWrite = Math.max(maxWrite, length);
            if (length > 12 && data[offset + 9] == 'r') { rows++; digest.update(data, offset, length); }
            if (length > 12 && data[offset + 9] == 'e') footer = new String(data, offset, length, StandardCharsets.UTF_8);
        }
    }

    private static final class Fake {
        final String[] names;
        final int[] types;
        final int count;
        final IntFunction<Object[]> values;
        final List<String> sqls = new ArrayList<>();
        final List<Integer> timeouts = new ArrayList<>();
        String fault = "";
        String executedSql = "";
        boolean enforceServerBound, locator;
        int driverMaterializedChars;
        boolean readOnly, forwardOnly, readOnlyCursor, rowClosed, rowCursorClosedBeforeCount, countClosed, endlessText;
        int fetchSize, textReads, charactersRead, timestampScale = 9;
        Runnable onNext = () -> { };
        Runnable onCount = () -> { };
        Fake(String[] names, int[] types, int count, IntFunction<Object[]> values) {
            this.names = names; this.types = types; this.count = count; this.values = values;
        }
        Connection connection() {
            return proxy(Connection.class, (p, m, a) -> switch (m.getName()) {
                case "setReadOnly" -> { readOnly = (boolean) a[0]; yield null; }
                case "prepareStatement" -> {
                    String sql = (String) a[0]; sqls.add(sql);
                    if (!readOnly) throw new AssertionError("query before read-only");
                    int kind = sql.contains("JOURNALED_OBJECTS") ? 0 : sql.contains("COUNT(*)") ? 2 : 1;
                    if (kind == 1) {
                        forwardOnly = a.length == 3 && (int) a[1] == ResultSet.TYPE_FORWARD_ONLY;
                        readOnlyCursor = a.length == 3 && (int) a[2] == ResultSet.CONCUR_READ_ONLY;
                    }
                    if (kind == 2) { rowCursorClosedBeforeCount = rowClosed; onCount.run(); }
                    yield statement(kind, sql);
                }
                case "close" -> null;
                default -> throw new AssertionError(m.getName());
            });
        }
        PreparedStatement statement(int kind, String sql) {
            return proxy(PreparedStatement.class, (p, m, a) -> switch (m.getName()) {
                case "setString" -> { check(kind == 0, "bindings only for journal metadata"); yield null; }
                case "setQueryTimeout" -> { timeouts.add((int) a[0]); yield null; }
                case "setFetchSize" -> { fetchSize = (int) a[0]; yield null; }
                case "getMetaData" -> fault.equals("no-metadata") ? null : metadata(flagName(sql));
                case "executeQuery" -> {
                    if (kind == 1) {
                        executedSql = sql;
                        if (enforceServerBound && !sql.contains("CASE WHEN")) {
                            if (!locator) driverMaterializedChars = 17;
                            throw new AssertionError("unbounded data reaches inline/locator driver before application Reader");
                        }
                        if (!locator && count > 0) {
                            for (Object cell : projected(0, sql)) {
                                if (cell instanceof String text) driverMaterializedChars += text.length();
                            }
                        }
                    }
                    yield result(kind, sql);
                }
                case "close" -> null;
                default -> throw new AssertionError(m.getName());
            });
        }
        private static String flagName(String sql) {
            var match = java.util.regex.Pattern.compile("THEN 0 ELSE 1 END AS (QDT_SOURCE_OVERSIZE_[0-9]+)").matcher(sql);
            return match.find() ? match.group(1) : null;
        }
        boolean safe(int index, String sql) {
            if (flagName(sql) == null) return true;
            var match = java.util.regex.Pattern.compile("<= ([0-9]+)").matcher(sql);
            long cellLimit = 0, rowLimit = 0;
            if (match.find()) cellLimit = rowLimit = Long.parseLong(match.group(1));
            while (match.find()) rowLimit = Long.parseLong(match.group(1));
            long total = 0;
            Object[] row = values.apply(index);
            for (int i = 0; i < types.length; i++) {
                if (textType(types[i]) && row[i] != null) {
                    long length = ((String) row[i]).getBytes(switch (types[i]) {
                        case Types.NCHAR, Types.NVARCHAR, Types.LONGNVARCHAR, Types.NCLOB -> StandardCharsets.UTF_16BE;
                        default -> StandardCharsets.UTF_8;
                    }).length;
                    if (length > cellLimit) return false;
                    total += length;
                }
            }
            return total <= rowLimit;
        }
        Object[] projected(int index, String sql) {
            Object[] row = values.apply(index);
            if (!safe(index, sql)) {
                for (int i = 0; i < types.length; i++) if (textType(types[i])) row[i] = null;
            }
            return row;
        }
        private static boolean textType(int type) {
            return switch (type) {
                case Types.CHAR, Types.VARCHAR, Types.LONGVARCHAR, Types.NCHAR, Types.NVARCHAR,
                     Types.LONGNVARCHAR, Types.CLOB, Types.NCLOB -> true;
                default -> false;
            };
        }
        ResultSet result(int kind, String sql) {
            int[] index = {-1};
            return proxy(ResultSet.class, (p, m, a) -> switch (m.getName()) {
                case "next" -> {
                    if (kind == 1) { onNext.run(); if (fault.equals("row-error")) throw new SQLException("secret"); }
                    int total = kind == 1 ? count : kind == 0 && fault.equals("journal-missing") ? 0 :
                            kind == 0 && fault.equals("journal-extra") || kind == 2 && fault.equals("count-extra") ? 2 : 1;
                    yield ++index[0] < total;
                }
                case "getMetaData" -> metadata(flagName(sql));
                case "getCharacterStream" -> {
                    textReads += kind == 1 ? 1 : 0;
                    if (kind == 0) yield new StringReader((int) a[0] == 1 ? "TESTLIB  " : fault.equals("journal-other") ? "OTHER" : "JRN  ");
                    Object value = projected(index[0], sql)[(int) a[0] - 1];
                    if (value == null) yield null;
                    if (endlessText) yield new Reader() {
                        public int read(char[] dst, int off, int len) {
                            java.util.Arrays.fill(dst, off, off + len, 'x'); charactersRead += len; return len;
                        }
                        public void close() { }
                    };
                    yield new StringReader((String) value);
                }
                case "getBigDecimal" -> {
                    if (kind == 2) yield fault.equals("count-null") ? null : BigDecimal.valueOf(count
                            + (fault.equals("count-small") ? -1 : fault.equals("count-large") ? 1 : 0));
                    if ((int) a[0] == names.length + 1) yield fault.equals("flag-null") ? null
                            : fault.equals("flag-two") ? BigDecimal.valueOf(2)
                            : fault.equals("flag-one") || !safe(index[0], sql) ? BigDecimal.ONE : BigDecimal.ZERO;
                    Object value = projected(index[0], sql)[(int) a[0] - 1];
                    yield value == null ? null : new BigDecimal(value.toString());
                }
                case "getDate", "getTimestamp" -> values.apply(index[0])[(int) a[0] - 1];
                case "close" -> {
                    if (kind == 1) rowClosed = true;
                    if (kind == 2) { countClosed = true; if (fault.equals("close-error")) throw new SQLException("secret"); }
                    yield null;
                }
                default -> throw new AssertionError("unbounded or unexpected getter: " + m.getName());
            });
        }
        ResultSetMetaData metadata(String flag) {
            return proxy(ResultSetMetaData.class, (p, m, a) -> switch (m.getName()) {
                case "getColumnCount" -> names.length + (flag != null ? 1 : 0);
                case "getColumnLabel" -> (int) a[0] == names.length + 1 ? flag
                        : fault.equals("metadata-name") ? "WRONG" : names[(int) a[0] - 1];
                case "getColumnType" -> (int) a[0] == names.length + 1 ? Types.INTEGER : types[(int) a[0] - 1];
                case "getPrecision" -> 38;
                case "getScale" -> types[(int) a[0] - 1] == Types.TIMESTAMP ? timestampScale : 2;
                default -> throw new AssertionError(m.getName());
            });
        }
    }

    private static <T> T proxy(Class<T> type, java.lang.reflect.InvocationHandler handler) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[]{type}, handler));
    }
}
