package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;

/** Offline contracts execute the real catalog/identity writers through fake JDBC. */
public final class NativeVerificationReceiverExportTest {
    private static final String SHA = "a".repeat(64);
    private static final byte[] INPUT = "offline.invalid\nREADER\nfixture-password\n".getBytes(StandardCharsets.UTF_8);
    private static int checks;

    private NativeVerificationReceiverExportTest() { }

    public static void main(String[] args) throws Exception {
        if (args.length == 2 && "--emit-fixture".equals(args[0])) {
            Fake jdbc = new Fake();
            int status = invoke(jdbc, arguments(args[1]), INPUT, System.out, System.err);
            if (status != 0) throw new AssertionError("fixture failed");
            return;
        }
        exactOriginalsAndReadOnlyQueries();
        inputAndTlsRefusals();
        errorsAndReplacement();
        System.out.println("native_verification_receiver_export=PASS checks=" + checks);
    }

    private static String[] arguments(String sha) {
        return new String[]{"JRNLIB", "JRN", "RCVLIB", "R_Z", "1", sha, "before"};
    }

    private static int invoke(Fake jdbc, String[] args, byte[] input, OutputStream out, OutputStream errors) {
        return NativeVerificationReceiverExport.run(args, new ByteArrayInputStream(input), out, errors,
            (host, user, password) -> {
                check(host.equals("offline.invalid") && user.equals("READER") && password.equals("fixture-password"),
                      "stdin credentials preserved");
                jdbc.connected = true;
                return jdbc.connection();
            });
    }

    private static void exactOriginalsAndReadOnlyQueries() throws Exception {
        Fake jdbc = new Fake();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream errors = new ByteArrayOutputStream();
        equal(0, invoke(jdbc, arguments(SHA), INPUT, out, errors));
        check(jdbc.readOnly && jdbc.closed, "read-only connection closed");
        equal(3, jdbc.statements.size());
        check(jdbc.statements.get(0).contains("IFS_OBJECT_STATISTICS")
              && jdbc.statements.get(1).contains("JOURNAL_RECEIVER_INFO")
              && jdbc.statements.get(2).contains("IFS_OBJECT_STATISTICS"), "identity/catalog/identity ordering");
        equal(Map.of(1, "JRNLIB", 2, "JRN", 3, "JRNLIB", 4, "JRN", 5, "R_Z"), jdbc.parameters.get(1));
        equal(3, jdbc.closedStatements);
        equal(3, jdbc.closedResults);
        check(jdbc.timeouts.stream().allMatch(value -> value >= 1 && value <= 29), "bounded SQL queries");
        String identity = "as400-receiver-identity-v1\nidentity\tJRNLIB\tJRN\tRCVLIB\tR_Z\t1\t123\t456\t789\n";
        String catalog = "as400-receiver-catalog-v2\nreceiver\tRCVLIB\tR_Z\tATTACHED\t1\t30\t-\t-\t-\t-\n";
        String text = out.toString(StandardCharsets.UTF_8);
        check(text.contains("\"identity_before_base64\":\"" + encode(identity) + "\""), "original before identity");
        check(text.contains("\"catalog_base64\":\"" + encode(catalog) + "\""), "original catalog");
        check(text.contains("\"identity_after_base64\":\"" + encode(identity) + "\""), "original after identity");
        check(text.endsWith("}\n") && errors.size() == 0 && !text.contains("fixture-password"), "complete safe output");
        Properties properties = NativeVerificationReceiverExport.connectionProperties("READER", "fixture-password");
        equal("true", properties.getProperty("secure"));
        equal("read only", properties.getProperty("access"));
        equal("false", properties.getProperty("prompt"));
    }

    private static void inputAndTlsRefusals() {
        for (String[] args : List.of(new String[]{}, new String[]{"bad"}, arguments("bad"))) {
            refusedBeforeConnection(args, INPUT);
        }
        for (int index : new int[]{0, 1, 2, 3, 4, 6}) {
            String[] args = arguments(SHA);
            args[index] = "invalid/field";
            refusedBeforeConnection(args, INPUT);
        }
        for (byte[] input : List.of(new byte[0], "offline.invalid\nREADER\n".getBytes(StandardCharsets.UTF_8),
                "offline.invalid/evil\nREADER\nfixture-password\n".getBytes(StandardCharsets.UTF_8),
                "offline.invalid\nREADER\nfixture-password\nextra\n".getBytes(StandardCharsets.UTF_8),
                "offline.invalid\nREADER\n\n".getBytes(StandardCharsets.UTF_8), new byte[]{(byte) 255, 10})) {
            refusedBeforeConnection(arguments(SHA), input);
        }
        for (String property : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String previous = System.getProperty(property);
            try {
                System.setProperty(property, "true");
                refusedBeforeConnection(arguments(SHA), INPUT);
            } finally {
                if (previous == null) System.clearProperty(property);
                else System.setProperty(property, previous);
            }
        }
    }

    private static void refusedBeforeConnection(String[] args, byte[] input) {
        Fake jdbc = new Fake();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream errors = new ByteArrayOutputStream();
        check(invoke(jdbc, args, input, out, errors) != 0, "invalid CLI refused");
        check(!jdbc.connected && out.size() == 0 && !errors.toString(StandardCharsets.UTF_8).contains("fixture-password"),
              "refused before opening source");
    }

    private static void errorsAndReplacement() {
        for (String fault : List.of("generation", "sql", "ambiguous", "null", "fraction", "oversize")) {
            Fake jdbc = new Fake();
            jdbc.fault = fault;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            ByteArrayOutputStream errors = new ByteArrayOutputStream();
            check(invoke(jdbc, arguments(SHA), INPUT, out, errors) != 0, "native failure refused: " + fault);
            check(jdbc.closed && out.size() == 0, "failure closes and emits no partial document");
            check(!errors.toString(StandardCharsets.UTF_8).contains("private-driver-message"), "safe SQL failure");
        }
    }

    private static String encode(String text) {
        return Base64.getEncoder().encodeToString(text.getBytes(StandardCharsets.UTF_8));
    }

    private static final class Fake {
        private final List<String> statements = new ArrayList<>();
        private final List<Map<Integer, String>> parameters = new ArrayList<>();
        private final List<Integer> timeouts = new ArrayList<>();
        private String fault = "";
        private boolean connected, readOnly, closed;
        private int closedStatements, closedResults;

        private Connection connection() {
            return proxy(Connection.class, (object, method, args) -> {
                return switch (method.getName()) {
                    case "setReadOnly" -> { readOnly = Boolean.TRUE.equals(args[0]); yield null; }
                    case "close" -> { closed = true; yield null; }
                    case "prepareStatement" -> statement((String) args[0]);
                    default -> throw new AssertionError(method.getName());
                };
            });
        }

        private PreparedStatement statement(String sql) {
            check(sql.strip().startsWith("SELECT"), "SQL is read-only");
            statements.add(sql);
            int index = statements.size() - 1;
            Map<Integer, String> values = new HashMap<>();
            parameters.add(values);
            return proxy(PreparedStatement.class, (object, method, args) -> {
                return switch (method.getName()) {
                    case "setString" -> { values.put((Integer) args[0], (String) args[1]); yield null; }
                    case "setQueryTimeout" -> { timeouts.add((Integer) args[0]); yield null; }
                    case "close" -> { closedStatements++; yield null; }
                    case "executeQuery" -> {
                        if (fault.equals("sql")) throw new SQLException("private-driver-message fixture-password");
                        yield result(sql.contains("IFS_OBJECT_STATISTICS"), index);
                    }
                    default -> throw new AssertionError(method.getName());
                };
            });
        }

        private ResultSet result(boolean identity, int queryIndex) {
            int[] row = {0};
            return proxy(ResultSet.class, (object, method, args) -> {
                return switch (method.getName()) {
                    case "next" -> ++row[0] <= (identity && fault.equals("ambiguous") ? 2 : 1);
                    case "close" -> { closedResults++; yield null; }
                    case "getBigDecimal" -> {
                        String name = (String) args[0];
                        if (fault.equals("null")) yield null;
                        if (fault.equals("fraction")) yield new BigDecimal("1.5");
                        yield new BigDecimal(switch (name) {
                            case "ASP_NUMBER", "FIRST_SEQUENCE_NUMBER" -> "1";
                            case "FILE_SYSTEM_IDENTIFIER" -> "123";
                            case "FILE_IDENTIFIER_NUMBER" -> "456";
                            case "GENERATION_IDENTIFIER" -> fault.equals("generation") && queryIndex == 2 ? "790" : "789";
                            case "LAST_SEQUENCE_NUMBER" -> "30";
                            default -> throw new AssertionError(name);
                        });
                    }
                    case "getString" -> switch ((String) args[0]) {
                        case "JOURNAL_RECEIVER_LIBRARY" -> "RCVLIB";
                        case "JOURNAL_RECEIVER_NAME" -> "R_Z";
                        case "STATUS" -> fault.equals("oversize") ? "x".repeat(300_000) : "ATTACHED";
                        case "PREVIOUS_JOURNAL_RECEIVER_LIBRARY", "PREVIOUS_JOURNAL_RECEIVER",
                             "NEXT_JOURNAL_RECEIVER_LIBRARY", "NEXT_JOURNAL_RECEIVER" -> null;
                        default -> throw new AssertionError((String) args[0]);
                    };
                    default -> throw new AssertionError(method.getName());
                };
            });
        }
    }

    @SuppressWarnings("unchecked")
    private static <T> T proxy(Class<T> type, java.lang.reflect.InvocationHandler handler) {
        return (T) Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[]{type}, handler);
    }

    private static void check(boolean value, String message) {
        checks++;
        if (!value) throw new AssertionError(message);
    }

    private static void equal(Object expected, Object actual) {
        check(expected.equals(actual), "expected " + expected + ", got " + actual);
    }
}
