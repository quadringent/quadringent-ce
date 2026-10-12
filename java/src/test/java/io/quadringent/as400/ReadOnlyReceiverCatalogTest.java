package io.quadringent.as400;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Offline JDBC contracts for native receiver links and bounded catalog queries. */
public final class ReadOnlyReceiverCatalogTest {
    private static final String COLUMNS = """
            JOURNAL_RECEIVER_LIBRARY, JOURNAL_RECEIVER_NAME, STATUS,
            FIRST_SEQUENCE_NUMBER, LAST_SEQUENCE_NUMBER,
            PREVIOUS_JOURNAL_RECEIVER_LIBRARY, PREVIOUS_JOURNAL_RECEIVER,
            NEXT_JOURNAL_RECEIVER_LIBRARY, NEXT_JOURNAL_RECEIVER
            """;
    private static final List<String> FAILURES = new ArrayList<>();

    private ReadOnlyReceiverCatalogTest() {
    }

    public static void main(String[] args) throws Exception {
        test("default query", ReadOnlyReceiverCatalogTest::testDefaultQuery);
        test("anchored query", ReadOnlyReceiverCatalogTest::testAnchoredQuery);
        test("native links and ordering", ReadOnlyReceiverCatalogTest::testNativeLinksAndOrdering);
        test("missing links", ReadOnlyReceiverCatalogTest::testMissingLinks);
        test("missing anchor", ReadOnlyReceiverCatalogTest::testMissingAnchor);
        test("blank anchor", ReadOnlyReceiverCatalogTest::testBlankAnchor);
        test("tail format", ReadOnlyReceiverCatalogTest::testTailFormat);
        if (!FAILURES.isEmpty()) {
            throw new AssertionError(String.join("\n", FAILURES));
        }
        System.out.println("ReadOnlyReceiverCatalogTest passed (7 contracts)");
    }

    private static void testDefaultQuery() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of());
        catalog(jdbc, 7, null);
        expectCatalogColumns(jdbc.sql);
        expect("default ordering and bound", normalized(jdbc.sql).endsWith(
                "ORDER BY ATTACH_TIMESTAMP DESC FETCH FIRST 7 ROWS ONLY"));
        expect("no default anchor", !jdbc.sql.contains("MIN(ATTACH_TIMESTAMP)"));
        equal(Map.of(1, "JRNLIB", 2, "DEMOJRN"), jdbc.parameters);
        expectClosedAndBounded(jdbc);
    }

    private static void testAnchoredQuery() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of());
        catalog(jdbc, 7, "  R_OLD  ");
        expectCatalogColumns(jdbc.sql);
        expect("checkpoint attachment bound", normalized(jdbc.sql).contains(
                "ATTACH_TIMESTAMP >= ( SELECT MIN(ATTACH_TIMESTAMP)"));
        expect("anchor receiver filter", jdbc.sql.contains("AND JOURNAL_RECEIVER_NAME = ?"));
        expect("anchored ordering and bound", normalized(jdbc.sql).endsWith(
                "ORDER BY ATTACH_TIMESTAMP DESC FETCH FIRST 1000 ROWS ONLY"));
        equal(Map.of(1, "JRNLIB", 2, "DEMOJRN", 3, "JRNLIB", 4, "DEMOJRN", 5, "R_OLD"),
                jdbc.parameters);
        expectClosedAndBounded(jdbc);
    }

    private static void testNativeLinksAndOrdering() throws Exception {
        List<Map<String, Object>> newestFirst = List.of(
                receiver("LIB_NEW", "R_A", "ATTACHED", "301", "350",
                        "LIB_MID", "R_M", null, null),
                receiver("LIB_MID", "R_M", "ONLINE", "201", "300",
                        "LIB_OLD", "R_Z", "LIB_NEW", "R_A"),
                receiver("LIB_OLD", "R_Z", "ONLINE", "101", "200",
                        "LIB_PRE", "R_P", "LIB_MID", "R_M"));
        String expected = """
                as400-receiver-catalog-v2
                receiver\tLIB_OLD\tR_Z\tONLINE\t101\t200\tLIB_PRE\tR_P\tLIB_MID\tR_M
                receiver\tLIB_MID\tR_M\tONLINE\t201\t300\tLIB_OLD\tR_Z\tLIB_NEW\tR_A
                receiver\tLIB_NEW\tR_A\tATTACHED\t301\t350\tLIB_MID\tR_M\t-\t-
                """;
        for (String anchor : new String[]{null, "R_Z"}) {
            JdbcFake jdbc = new JdbcFake(newestFirst);
            equal(expected, catalog(jdbc, 3, anchor));
            expectClosedAndBounded(jdbc);
        }
    }

    private static void testMissingLinks() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of(
                receiver("LIB_ONE", "R_ONE", null, null, null, null, null, null, null),
                receiver("LIB_TWO", "R_TWO", "ONLINE", "1", "2", "LIB_PRE", null, null, "R_NEXT")));
        equal("""
                as400-receiver-catalog-v2
                receiver\tLIB_TWO\tR_TWO\tONLINE\t1\t2\tLIB_PRE\t-\t-\tR_NEXT
                receiver\tLIB_ONE\tR_ONE\t-\t-\t-\t-\t-\t-\t-
                """, catalog(jdbc, 2, null));
    }

    private static void testMissingAnchor() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of());
        equal("as400-receiver-catalog-v2\n", catalog(jdbc, 20, "R_MISSING"));
    }

    private static void testBlankAnchor() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of());
        equal("as400-receiver-catalog-v2\n", catalog(jdbc, 4, "  "));
        expect("blank anchor uses default bound", normalized(jdbc.sql).endsWith(
                "ORDER BY ATTACH_TIMESTAMP DESC FETCH FIRST 4 ROWS ONLY"));
        equal(Map.of(1, "JRNLIB", 2, "DEMOJRN"), jdbc.parameters);
    }

    private static void testTailFormat() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of(
                receiver("LIB_NEW", "R_A", "ATTACHED", "301", "350", null, null, null, null)));
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (PrintStream out = new PrintStream(bytes, true, StandardCharsets.UTF_8)) {
            ReadOnlyReceiverCatalog.writeTail(jdbc.connection(), "JRNLIB", "DEMOJRN", out);
        }
        equal("tail receiver=R_A library=LIB_NEW first_sequence=301 last_sequence=350 status=ATTACHED\n",
                bytes.toString(StandardCharsets.UTF_8));
        expect("attached tail only", jdbc.sql.contains("AND STATUS = 'ATTACHED'"));
        expect("tail bound", normalized(jdbc.sql).endsWith("FETCH FIRST 1 ROWS ONLY"));
        expectClosedAndBounded(jdbc);
    }

    private static String catalog(JdbcFake jdbc, int limit, String anchor) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (PrintStream out = new PrintStream(bytes, true, StandardCharsets.UTF_8)) {
            ReadOnlyReceiverCatalog.writeReceivers(jdbc.connection(), "JRNLIB", "DEMOJRN", limit, anchor, out);
        }
        return bytes.toString(StandardCharsets.UTF_8);
    }

    private static void expectCatalogColumns(String sql) {
        String normalizedSql = normalized(sql);
        equal("SELECT " + normalized(COLUMNS), normalizedSql.substring(0, normalizedSql.indexOf(" FROM ")));
        expect("no entry payload", !sql.contains("ENTRY_DATA"));
        expect("journal filter", normalizedSql.contains("WHERE JOURNAL_LIBRARY = ? AND JOURNAL_NAME = ?"));
    }

    private static void expectClosedAndBounded(JdbcFake jdbc) {
        equal(ReadOnlyReceiverCatalog.catalogQueryTimeoutSeconds(), jdbc.timeout);
        expect("bounded timeout", jdbc.timeout >= 1 && jdbc.timeout < 30);
        expect("result closed", jdbc.resultClosed);
        expect("statement closed", jdbc.statementClosed);
    }

    private static String normalized(String value) {
        return value.trim().replaceAll("\\s+", " ");
    }

    private static Map<String, Object> receiver(
            String library, String name, String status, String first, String last,
            String previousLibrary, String previousName, String nextLibrary, String nextName) {
        Map<String, Object> row = new HashMap<>();
        row.put("JOURNAL_RECEIVER_LIBRARY", library);
        row.put("JOURNAL_RECEIVER_NAME", name);
        row.put("STATUS", status);
        row.put("FIRST_SEQUENCE_NUMBER", first == null ? null : new BigDecimal(first));
        row.put("LAST_SEQUENCE_NUMBER", last == null ? null : new BigDecimal(last));
        row.put("PREVIOUS_JOURNAL_RECEIVER_LIBRARY", previousLibrary);
        row.put("PREVIOUS_JOURNAL_RECEIVER", previousName);
        row.put("NEXT_JOURNAL_RECEIVER_LIBRARY", nextLibrary);
        row.put("NEXT_JOURNAL_RECEIVER", nextName);
        return row;
    }

    private static final class JdbcFake {
        private final List<Map<String, Object>> rows;
        private final Map<Integer, Object> parameters = new HashMap<>();
        private String sql;
        private int timeout;
        private int rowIndex = -1;
        private boolean resultClosed;
        private boolean statementClosed;

        private JdbcFake(List<Map<String, Object>> rows) {
            this.rows = rows;
        }

        private Connection connection() {
            return proxy(Connection.class, (connection, method, args) -> {
                if (!method.getName().equals("prepareStatement")) {
                    throw new AssertionError(method.getName());
                }
                sql = (String) args[0];
                return proxy(PreparedStatement.class, (statement, call, values) -> {
                    switch (call.getName()) {
                        case "setString": parameters.put((Integer) values[0], values[1]); return null;
                        case "setQueryTimeout": timeout = (Integer) values[0]; return null;
                        case "close": statementClosed = true; return null;
                        case "executeQuery": return proxy(ResultSet.class, (result, read, fields) -> {
                            switch (read.getName()) {
                                case "next": return ++rowIndex < rows.size();
                                case "getString", "getBigDecimal":
                                    String column = (String) fields[0];
                                    expect("declared result column " + column, rows.get(rowIndex).containsKey(column));
                                    return rows.get(rowIndex).get(column);
                                case "close": resultClosed = true; return null;
                                default: throw new AssertionError(read.getName());
                            }
                        });
                        default: throw new AssertionError(call.getName());
                    }
                });
            });
        }
    }

    private static <T> T proxy(Class<T> type, InvocationHandler handler) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[]{type}, handler));
    }

    private static void test(String name, Checked action) throws Exception {
        try {
            action.run();
        }
        catch (AssertionError failure) {
            FAILURES.add(name + ": " + failure.getMessage());
        }
    }

    private static void equal(Object expected, Object actual) {
        if (!expected.equals(actual)) {
            throw new AssertionError(expected + " != " + actual);
        }
    }

    private static void expect(String name, boolean condition) {
        if (!condition) {
            throw new AssertionError(name);
        }
    }

    private interface Checked {
        void run() throws Exception;
    }
}
