package io.quadringent.as400;

import java.util.List;
import java.nio.charset.StandardCharsets;

/** Offline safety checks for the dedicated IBM i qualification source CLI. */
public final class QualificationSourceDriverTest {
    private static int checks;

    public static void main(String[] args) throws Exception {
        var scope = new QualificationSourceDriver.Scope(
                "QUALTEST", "QUALIF_ORDERS", "QUALTEST", "QUALJRN", "ORDER_ID",
                List.of("ORDER_ID", "LABEL"));
        accepts(scope, "INSERT INTO QUALTEST.QUALIF_ORDERS (ORDER_ID, LABEL) VALUES (1, 'Café')");
        accepts(scope, "UPDATE QUALTEST.QUALIF_ORDERS SET LABEL = 'Après' WHERE ORDER_ID = 1");
        accepts(scope, "DELETE FROM QUALTEST.QUALIF_ORDERS WHERE ORDER_ID = 1");
        refuses(scope, "DELETE FROM QUALTEST.BUSINESS_ORDERS WHERE ORDER_ID = 1");
        refuses(scope, "DELETE FROM QUALTEST.QUALIF_ORDERS WHERE ORDER_ID = 1; DROP TABLE X");
        refuses(scope, "UPDATE QUALTEST.QUALIF_ORDERS SET LABEL = 'x' -- comment WHERE ORDER_ID = 1");
        refuses(scope, "SELECT * FROM QUALTEST.QUALIF_ORDERS");
        if (!QualificationSourceDriver.journalMatches(scope, "QUALTEST", "QUALJRN")) {
            throw new AssertionError("configured journal should match the source table");
        }
        checks++;
        if (QualificationSourceDriver.journalMatches(scope, "QUALTEST", "OTHERJRN")
                || QualificationSourceDriver.journalMatches(scope, "OTHERLIB", "QUALJRN")) {
            throw new AssertionError("journal outside source table must be refused");
        }
        checks++;
        equal("RCVLIB", QualificationSourceDriver.receiverLibraryForStart(
                List.of(new QualificationSourceDriver.ReceiverRef("RCVLIB", "RCV0001")), "RCV0001"));
        rejects(() -> QualificationSourceDriver.receiverLibraryForStart(
                List.of(new QualificationSourceDriver.ReceiverRef("RCVLIB", "RCV0001")), "OTHER"));
        rejects(() -> QualificationSourceDriver.receiverLibraryForStart(
                List.of(new QualificationSourceDriver.ReceiverRef("RCVLIB", "RCV0001"),
                        new QualificationSourceDriver.ReceiverRef("OTHERLIB", "RCV0001")), "RCV0001"));
        equal("\"line\\nquote\\\"slash\\\\\"", QualificationSourceDriver.jsonString("line\nquote\"slash\\"));
        rejects(() -> new QualificationSourceDriver.Scope(
                "QUALTEST;DROP", "QUALIF_ORDERS", "QUALTEST", "QUALJRN", "ORDER_ID",
                List.of("ORDER_ID")));
        rejects(() -> new QualificationSourceDriver.Scope(
                "QUALTEST", "QUALIF_ORDERS", "QUALTEST", "QUALJRN", "ORDER_ID",
                List.of("LABEL")));
        freshnessWorkerContract(scope);
        typedTemporalDump();
        rotationCall(scope, false);
        rotationCall(scope, true);
        executionCall(false, 0, true);
        executionCall(false, 2, true);
        executionCall(true, 0, true);
        executionCall(true, 2, true);
        executionCall(true, 0, false);
        System.out.println("qualification_source_driver=PASS checks=" + checks);
    }

    private static void freshnessWorkerContract(QualificationSourceDriver.Scope scope) throws Exception {
        String run = "8d291bee-a837-48fb-aba6-f04f643e13c4";
        List<String> expected = new java.util.ArrayList<>();
        for (int i = 1; i <= 3; i++) expected.add(QualificationSourceDriver.freshnessSql(scope, run, i));
        rejects(() -> QualificationSourceDriver.freshnessSql(scope, "not-a-uuid", 1));
        rejects(() -> QualificationSourceDriver.freshnessSql(scope, run, 4));
        for (String commands : List.of("", "FRESHNESS_EXEC=1\nFRESHNESS_EXEC=2\nFRESHNESS_EXEC=3\n",
                "FRESHNESS_EXEC=2\n", "FRESHNESS_EXEC=1\nFRESHNESS_EXEC=1\n",
                "FRESHNESS_EXEC=1\nFRESHNESS_EXEC=2\nFRESHNESS_EXEC=3\nFRESHNESS_EXEC=4\n")) {
            List<String> events = new java.util.ArrayList<>();
            var statement = (java.sql.Statement) java.lang.reflect.Proxy.newProxyInstance(
                    QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.Statement.class},
                    (proxy, method, arguments) -> switch (method.getName()) {
                        case "setQueryTimeout", "close" -> null;
                        case "executeQuery" -> {
                            equal("SELECT 1 FROM SYSIBM.SYSDUMMY1", (String) arguments[0]);
                            events.add("SELECT1");
                            int[] count = {0};
                            yield java.lang.reflect.Proxy.newProxyInstance(
                                    QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.ResultSet.class},
                                    (p, m, a) -> switch (m.getName()) {
                                        case "next" -> ++count[0] == 1;
                                        case "getInt" -> 1;
                                        case "close" -> null;
                                        default -> throw new AssertionError(m.getName());
                                    });
                        }
                        case "executeUpdate" -> {
                            int index = events.size() - 1;
                            equal(expected.get(index), (String) arguments[0]);
                            events.add("UPDATE");
                            yield 1;
                        }
                        default -> throw new AssertionError(method.getName());
                    });
            var connection = (java.sql.Connection) java.lang.reflect.Proxy.newProxyInstance(
                    QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.Connection.class},
                    (proxy, method, arguments) -> switch (method.getName()) {
                        case "createStatement" -> statement;
                        case "getAutoCommit" -> true;
                        default -> throw new AssertionError(method.getName());
                    });
            String payload = String.join("\n", expected) + "\n" + commands;
            var input = new java.io.BufferedReader(new java.io.StringReader(payload));
            var output = new java.io.ByteArrayOutputStream();
            var original = System.out;
            boolean invalid = false;
            try {
                System.setOut(new java.io.PrintStream(output, true, StandardCharsets.UTF_8));
                QualificationSourceDriver.freshnessWorker(connection, scope, input, run);
            } catch (IllegalArgumentException failure) {
                invalid = true;
            } finally {
                System.setOut(original);
            }
            boolean mustFail = commands.startsWith("FRESHNESS_EXEC=2")
                    || commands.contains("EXEC=1\nFRESHNESS_EXEC=1") || commands.contains("EXEC=4");
            if (invalid != mustFail) throw new AssertionError("command budget/order must fail closed");
            if (!events.get(0).equals("SELECT1")) throw new AssertionError("connection readiness before DML");
            if (!output.toString(StandardCharsets.UTF_8).startsWith("SRC_FRESHNESS_READY=" + run + "\n")) {
                throw new AssertionError("readiness must precede ACK");
            }
            if (commands.isEmpty() && events.size() != 1) throw new AssertionError("EOF must not write");
            checks++;
        }
    }

    /** DATE typée et octets Unicode exacts, même avec un stdout configuré en ASCII. */
    private static void typedTemporalDump() throws Exception {
        int[] types = {java.sql.Types.DATE, java.sql.Types.TIMESTAMP,
                java.sql.Types.DATE, java.sql.Types.TIMESTAMP, java.sql.Types.VARCHAR};
        String[] names = {"EVENT_DATE", "UPDATED_AT", "NULL_DATE", "NULL_TS", "LABEL"};
        var metadata = (java.sql.ResultSetMetaData) java.lang.reflect.Proxy.newProxyInstance(
                QualificationSourceDriverTest.class.getClassLoader(),
                new Class<?>[]{java.sql.ResultSetMetaData.class}, (proxy, method, arguments) -> switch (method.getName()) {
                    case "getColumnCount" -> names.length;
                    case "getColumnLabel" -> names[(int) arguments[0] - 1];
                    case "getColumnType" -> types[(int) arguments[0] - 1];
                    default -> throw new AssertionError(method.getName());
                });
        int[] next = {0};
        var rows = (java.sql.ResultSet) java.lang.reflect.Proxy.newProxyInstance(
                QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.ResultSet.class},
                (proxy, method, arguments) -> switch (method.getName()) {
                    case "getMetaData" -> metadata;
                    case "next" -> next[0]++ == 0;
                    case "getString" -> switch ((int) arguments[0]) {
                        case 1 -> "26-01-02";
                        case 2 -> "26-01-02 03:04:05.123456";
                        case 5 -> "Modifié n°1 — Große 日本語";
                        default -> null;
                    };
                    case "getDate" -> (int) arguments[0] == 1 ? java.sql.Date.valueOf("2026-01-02") : null;
                    case "getTimestamp" -> (int) arguments[0] == 2
                            ? java.sql.Timestamp.valueOf("2026-01-02 03:04:05.123456") : null;
                    default -> throw new AssertionError(method.getName());
                });
        var output = new java.io.ByteArrayOutputStream();
        var previous = System.out;
        try (var stream = new java.io.PrintStream(output, true, java.nio.charset.StandardCharsets.US_ASCII)) {
            System.setOut(stream);
            var emit = QualificationSourceDriver.class.getDeclaredMethod("emitRows",
                    java.sql.ResultSet.class, String.class, int.class);
            emit.setAccessible(true);
            emit.invoke(null, rows, "SRC_ROW", 10);
        } finally { System.setOut(previous); }
        equal("SRC_ROW={\"EVENT_DATE\":\"2026-01-02\",\"UPDATED_AT\":\"2026-01-02T03:04:05.123456\","
                + "\"NULL_DATE\":null,\"NULL_TS\":null,\"LABEL\":\"Modifié n°1 — Große 日本語\"}\nSRC_ROW_COUNT=1\n",
                output.toString(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static void rotationCall(QualificationSourceDriver.Scope scope, boolean fail) throws Exception {
        var calls = new java.util.ArrayList<String>();
        var statement = (java.sql.CallableStatement) java.lang.reflect.Proxy.newProxyInstance(
                QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.CallableStatement.class},
                (proxy, method, args) -> {
                    switch (method.getName()) {
                        case "setString" -> calls.add("string:" + args[0] + ":" + args[1]);
                        case "setBigDecimal" -> calls.add("decimal:" + args[0] + ":" + args[1]);
                        case "setQueryTimeout" -> calls.add("timeout:" + args[0]);
                        case "execute" -> { calls.add("execute"); if (fail) throw new java.sql.SQLException("offline"); return false; }
                        case "close" -> { return null; }
                        default -> throw new AssertionError(method.getName());
                    }
                    return null;
                });
        var connection = (java.sql.Connection) java.lang.reflect.Proxy.newProxyInstance(
                QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.Connection.class},
                (proxy, method, args) -> {
                    if (!method.getName().equals("prepareCall")) throw new AssertionError(method.getName());
                    equal("CALL QSYS2.QCMDEXC(CAST(? AS VARCHAR(32000)), CAST(? AS DECIMAL(15,5)))", (String) args[0]);
                    return statement;
                });
        var output = new java.io.ByteArrayOutputStream();
        var previous = System.out;
        boolean threw = false;
        try (var stream = new java.io.PrintStream(output, true, StandardCharsets.UTF_8)) {
            System.setOut(stream);
            try { QualificationSourceDriver.rotate(connection, scope); }
            catch (java.sql.SQLException expected) { threw = true; }
        } finally { System.setOut(previous); }
        String command = "CHGJRN JRN(QUALTEST/QUALJRN) JRNRCV(*GEN) SEQOPT(*CONT)";
        if (!calls.equals(List.of("string:1:" + command, "decimal:2:" + command.length() + ".00000", "timeout:60", "execute"))) {
            throw new AssertionError("typed unique command mismatch");
        }
        String text = output.toString(StandardCharsets.UTF_8);
        if (threw != fail || !text.startsWith("SRC_ROTATION_STARTED=")
                || text.contains("SRC_ROTATION_ACK=") == fail || text.contains("SRC_ROTATED=true") == fail) {
            throw new AssertionError("ambiguous rotation must never emit ACK");
        }
        String start = text.lines().findFirst().orElseThrow().substring("SRC_ROTATION_STARTED=".length());
        java.time.Instant.parse(start);
        if (!fail && !text.contains("\"started_utc\":\"" + start + "\"")) throw new AssertionError("ACK must bind actual START");
        if (!fail && (!text.contains("\"journal_library\":\"QUALTEST\"") || !text.contains("\"journal_name\":\"QUALJRN\""))) throw new AssertionError("ACK must bind actual journal scope");
        checks += 4;
    }

    /** ACK JDBC réel : après executeUpdate, avant close, jamais sur échec. */
    private static void executionCall(boolean timed, int failAt, boolean autoCommit) throws Exception {
        var output = new java.io.ByteArrayOutputStream();
        int[] created = {0};
        java.time.Instant[] entered = new java.time.Instant[2];
        java.time.Instant[] returned = new java.time.Instant[2];
        java.time.Instant[] closed = new java.time.Instant[2];
        var connection = (java.sql.Connection) java.lang.reflect.Proxy.newProxyInstance(
                QualificationSourceDriverTest.class.getClassLoader(), new Class<?>[]{java.sql.Connection.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("getAutoCommit")) return autoCommit;
                    if (!method.getName().equals("createStatement")) throw new AssertionError(method.getName());
                    int index = ++created[0];
                    return java.lang.reflect.Proxy.newProxyInstance(QualificationSourceDriverTest.class.getClassLoader(),
                            new Class<?>[]{java.sql.Statement.class}, (statement, operation, params) -> {
                                switch (operation.getName()) {
                                    case "setQueryTimeout" -> { equal("30", params[0].toString()); return null; }
                                    case "executeUpdate" -> {
                                        entered[index - 1] = java.time.Instant.now();
                                        if (output.toString(StandardCharsets.UTF_8).contains("\"statement_index\":" + index))
                                            throw new AssertionError("ACK emitted before actual executeUpdate return");
                                        if (index == failAt) throw new java.sql.SQLException("private SQL failure");
                                        returned[index - 1] = java.time.Instant.now();
                                        return index * 3;
                                    }
                                    case "close" -> {
                                        closed[index - 1] = java.time.Instant.now();
                                        boolean ack = output.toString(StandardCharsets.UTF_8).contains("\"statement_index\":" + index);
                                        if (ack != (timed && index != failAt))
                                            throw new AssertionError("ACK must follow successful return and precede close");
                                        checks++;
                                        return null;
                                    }
                                    default -> throw new AssertionError(operation.getName());
                                }
                            });
                });
        var previous = System.out;
        boolean threw = false;
        try (var stream = new java.io.PrintStream(output, true, StandardCharsets.UTF_8)) {
            System.setOut(stream);
            var method = QualificationSourceDriver.class.getDeclaredMethod(timed ? "executeTimed" : "execute",
                    java.sql.Connection.class, List.class);
            method.setAccessible(true);
            try { method.invoke(null, connection, List.of("private-statement-one", "private-statement-two")); }
            catch (java.lang.reflect.InvocationTargetException failure) {
                if (!(failure.getCause() instanceof java.sql.SQLException)) throw failure;
                threw = true;
            }
        } finally { System.setOut(previous); }
        String text = output.toString(StandardCharsets.UTF_8);
        boolean expectedFailure = failAt > 0 || (timed && !autoCommit);
        if (threw != expectedFailure || text.contains("SRC_EXEC_TOTAL=") == expectedFailure
                || text.contains("private") || text.contains("statement-one"))
            throw new AssertionError("partial execution must not claim full total or expose SQL");
        if (!timed) equal(expectedFailure ? "" : "SRC_EXEC_TOTAL=2\n", text);
        if (timed) {
            var pattern = java.util.regex.Pattern.compile("SRC_EXEC_ACK=\\{\"statement_index\":(\\d+),\"start_utc\":\"([^\"]+)\",\"ack_utc\":\"([^\"]+)\",\"elapsed_ns\":(\\d+),\"affected_rows\":(\\d+)\\}");
            var matches = pattern.matcher(text);
            int count = 0;
            while (matches.find()) {
                int index = Integer.parseInt(matches.group(1));
                if (index != ++count || index == failAt || !autoCommit) throw new AssertionError("false statement ACK");
                var start = java.time.Instant.parse(matches.group(2));
                var ack = java.time.Instant.parse(matches.group(3));
                if (start.isAfter(entered[index - 1]) || ack.isBefore(returned[index - 1])
                        || ack.isAfter(closed[index - 1]) || ack.isBefore(start)
                        || Long.parseLong(matches.group(4)) < 0 || Integer.parseInt(matches.group(5)) != index * 3)
                    throw new AssertionError("ACK timestamps must bound the actual JDBC call");
            }
            int expectedCount = !autoCommit ? 0 : failAt > 0 ? failAt - 1 : 2;
            if (count != expectedCount) throw new AssertionError("missing successful statement ACK");
            if (!expectedFailure && !text.endsWith("SRC_EXEC_TOTAL=2\n")) throw new AssertionError("total must remain last");
        }
        checks += 3;
    }

    private static void accepts(QualificationSourceDriver.Scope scope, String sql) {
        if (!QualificationSourceDriver.allowedDml(scope, sql)) throw new AssertionError(sql);
        checks++;
    }

    private static void refuses(QualificationSourceDriver.Scope scope, String sql) {
        if (QualificationSourceDriver.allowedDml(scope, sql)) throw new AssertionError(sql);
        checks++;
    }

    private static void equal(String expected, String actual) {
        if (!expected.equals(actual)) throw new AssertionError("JSON escape mismatch");
        checks++;
    }

    private static void rejects(Runnable action) {
        try { action.run(); }
        catch (IllegalArgumentException expected) { checks++; return; }
        throw new AssertionError("expected IllegalArgumentException");
    }
}
