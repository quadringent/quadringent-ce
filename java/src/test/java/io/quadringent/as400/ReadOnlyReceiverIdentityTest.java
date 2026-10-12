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
import java.sql.SQLException;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Offline JDBC behavior: exact object, complete identity, and bounded cleanup. */
public final class ReadOnlyReceiverIdentityTest {
    private static int contracts;

    private ReadOnlyReceiverIdentityTest() { }

    public static void main(String[] args) throws Exception {
        success();
        signedBigint();
        refusal(new JdbcFake(List.of()), IllegalStateException.class);
        refusal(new JdbcFake(List.of(row(), row())), IllegalStateException.class);
        for (String field : List.of("ASP_NUMBER", "FILE_SYSTEM_IDENTIFIER",
                "FILE_IDENTIFIER_NUMBER", "GENERATION_IDENTIFIER")) {
            Map<String, BigDecimal> incomplete = row();
            incomplete.put(field, null);
            refusal(new JdbcFake(List.of(incomplete)), IllegalStateException.class);
        }
        for (String value : List.of("789.1", "9223372036854775808", "-9223372036854775809")) {
            Map<String, BigDecimal> malformed = row();
            malformed.put("GENERATION_IDENTIFIER", new BigDecimal(value));
            refusal(new JdbcFake(List.of(malformed)), RuntimeException.class);
        }
        Map<String, BigDecimal> otherAsp = row();
        otherAsp.put("ASP_NUMBER", new BigDecimal("2"));
        refusal(new JdbcFake(List.of(otherAsp)), IllegalStateException.class);
        JdbcFake failed = new JdbcFake(List.of(row()));
        failed.sqlFailure = true;
        refusal(failed, SQLException.class);
        invalidRequests();
        System.out.println("ReadOnlyReceiverIdentityTest passed (" + contracts + " contracts)");
    }

    private static void success() throws Exception {
        JdbcFake jdbc = new JdbcFake(List.of(row()));
        equal("as400-receiver-identity-v1\nidentity\tJRNLIB\tJRN\tRCVLIB\tRCV\t1\t123\t456\t789\n",
                emit(jdbc));
        String path = "/QSYS.LIB/RCVLIB.LIB/RCV.JRNRCV";
        equal(Map.of(1, path, 2, path), jdbc.parameters);
        String sql = jdbc.sql.replaceAll("\\s+", " ").trim();
        check(sql.contains("SUBTREE_DIRECTORIES => 'NO'"));
        check(sql.contains("OBJECT_TYPE_LIST => '*JRNRCV'"));
        check(sql.contains("IGNORE_ERRORS => 'NO'"));
        check(sql.contains("WHERE CAST(PATH_NAME AS VARCHAR(1024)) = ?"));
        check(sql.endsWith("FETCH FIRST 2 ROWS ONLY"));
        check(!sql.contains("TIMESTAMP") && !sql.contains("ENTRY_DATA"));
        equal(ReadOnlyReceiverCatalog.catalogQueryTimeoutSeconds(), jdbc.timeout);
        check(jdbc.timeout > 0 && jdbc.timeout < 30 && jdbc.resultClosed && jdbc.statementClosed);
        contracts++;
    }

    private static void signedBigint() throws Exception {
        Map<String, BigDecimal> values = row();
        values.put("FILE_SYSTEM_IDENTIFIER", new BigDecimal("-9223372036854775808"));
        values.put("FILE_IDENTIFIER_NUMBER", new BigDecimal("9223372036854775807"));
        values.put("GENERATION_IDENTIFIER", BigDecimal.ZERO);
        check(emit(new JdbcFake(List.of(values))).endsWith(
                "\t1\t-9223372036854775808\t9223372036854775807\t0\n"));
        contracts++;
    }

    private static void refusal(JdbcFake jdbc, Class<? extends Exception> expected) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (PrintStream out = new PrintStream(bytes, true, StandardCharsets.UTF_8)) {
            try {
                ReadOnlyReceiverIdentity.writeIdentity(jdbc.connection(), "JRNLIB", "JRN",
                        new ReadOnlyReceiverIdentity.Request("RCVLIB", "RCV", 1), out);
                throw new AssertionError("unsafe identity was emitted");
            }
            catch (Exception error) {
                if (!expected.isInstance(error)) {
                    throw error;
                }
            }
        }
        check(bytes.size() == 0 && jdbc.statementClosed);
        check(jdbc.sqlFailure || jdbc.resultClosed);
        contracts++;
    }

    private static void invalidRequests() throws Exception {
        for (String value : new String[]{"", "LIB/OTHER", "TOOLONGNAME1", "LIB\n", "lib", null}) {
            try {
                new ReadOnlyReceiverIdentity.Request(value, "RCV", 1);
                throw new AssertionError("unsafe library accepted");
            }
            catch (IllegalArgumentException expected) { contracts++; }
        }
        for (int asp : new int[]{0, 2, 33}) {
            try {
                new ReadOnlyReceiverIdentity.Request("RCVLIB", "RCV", asp);
                throw new AssertionError("unsupported ASP accepted");
            }
            catch (IllegalArgumentException expected) { contracts++; }
        }
        Map<String, String> fields = Map.of("cmd", "receiver_identity", "receiver_library", "RCVLIB",
                "receiver_name", "RCV", "asp_number", "1");
        equal(new ReadOnlyReceiverIdentity.Request("RCVLIB", "RCV", 1),
                ReadOnlyReceiverIdentity.Request.fromFields(fields));
        for (String asp : List.of("01", "+1", "1.0", "true", "2")) {
            Map<String, String> changed = new HashMap<>(fields);
            changed.put("asp_number", asp);
            try {
                ReadOnlyReceiverIdentity.Request.fromFields(changed);
                throw new AssertionError("malformed ASP accepted");
            }
            catch (IllegalArgumentException expected) { contracts++; }
        }
        Map<String, String> extra = new HashMap<>(fields);
        extra.put("unexpected", "value");
        try {
            ReadOnlyReceiverIdentity.Request.fromFields(extra);
            throw new AssertionError("unexpected request field accepted");
        }
        catch (IllegalArgumentException expected) { contracts++; }
        JdbcFake jdbc = new JdbcFake(List.of(row()));
        try {
            ReadOnlyReceiverIdentity.writeIdentity(jdbc.connection(), "BAD/PATH", "JRN",
                    new ReadOnlyReceiverIdentity.Request("RCVLIB", "RCV", 1), System.out);
            throw new AssertionError("unsafe journal scope accepted");
        }
        catch (IllegalArgumentException expected) {
            check(jdbc.sql == null);
            contracts++;
        }
    }

    private static String emit(JdbcFake jdbc) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (PrintStream out = new PrintStream(bytes, true, StandardCharsets.UTF_8)) {
            ReadOnlyReceiverIdentity.writeIdentity(jdbc.connection(), "JRNLIB", "JRN",
                    new ReadOnlyReceiverIdentity.Request("RCVLIB", "RCV", 1), out);
        }
        return bytes.toString(StandardCharsets.UTF_8);
    }

    private static Map<String, BigDecimal> row() {
        return new HashMap<>(Map.of("ASP_NUMBER", BigDecimal.ONE,
                "FILE_SYSTEM_IDENTIFIER", new BigDecimal("123"),
                "FILE_IDENTIFIER_NUMBER", new BigDecimal("456"),
                "GENERATION_IDENTIFIER", new BigDecimal("789")));
    }

    private static final class JdbcFake {
        private final List<Map<String, BigDecimal>> rows;
        private final Map<Integer, String> parameters = new HashMap<>();
        private int index = -1;
        private String sql;
        private int timeout;
        private boolean resultClosed;
        private boolean statementClosed;
        private boolean sqlFailure;

        private JdbcFake(List<Map<String, BigDecimal>> rows) { this.rows = rows; }

        private Connection connection() {
            return proxy(Connection.class, (connection, method, args) -> {
                if (!method.getName().equals("prepareStatement")) {
                    throw new AssertionError(method.getName());
                }
                sql = (String) args[0];
                return proxy(PreparedStatement.class, (statement, call, values) -> {
                    switch (call.getName()) {
                        case "setString": parameters.put((Integer) values[0], (String) values[1]); return null;
                        case "setQueryTimeout": timeout = (Integer) values[0]; return null;
                        case "close": statementClosed = true; return null;
                        case "executeQuery":
                            if (sqlFailure) { throw new SQLException("private endpoint diagnostic"); }
                            return proxy(ResultSet.class, (result, read, fields) -> {
                                switch (read.getName()) {
                                    case "next": return ++index < rows.size();
                                    case "getBigDecimal": return rows.get(index).get((String) fields[0]);
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

    private static void equal(Object expected, Object actual) {
        if (!expected.equals(actual)) { throw new AssertionError(expected + " != " + actual); }
    }

    private static void check(boolean value) {
        if (!value) { throw new AssertionError("receiver identity contract failed"); }
    }
}
