package io.quadringent.as400;

import java.io.PrintStream;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.Map;

/** Exact read-only object identity; attachment timestamps are not generations. */
public final class ReadOnlyReceiverIdentity {
    private static final BigInteger MIN_ID = BigInteger.valueOf(Long.MIN_VALUE);
    private static final BigInteger MAX_ID = BigInteger.valueOf(Long.MAX_VALUE);

    private ReadOnlyReceiverIdentity() { }

    public record Request(String library, String name, int aspNumber) {
        public Request {
            identifier(library);
            identifier(name);
            if (aspNumber != 1) {
                throw new IllegalArgumentException("only explicit system ASP 1 is supported");
            }
        }

        static Request fromFields(Map<String, String> fields) {
            if (!fields.keySet().equals(java.util.Set.of(
                    "cmd", "receiver_library", "receiver_name", "asp_number"))
                    || !"receiver_identity".equals(fields.get("cmd"))
                    || !"1".equals(fields.get("asp_number"))) {
                throw new IllegalArgumentException("invalid receiver identity request");
            }
            return new Request(fields.get("receiver_library"), fields.get("receiver_name"), 1);
        }
    }

    static void writeIdentity(
            Connection connection, String journalLibrary, String journalName,
            Request request, PrintStream out) throws Exception {
        identifier(journalLibrary);
        identifier(journalName);
        String path = "/QSYS.LIB/" + request.library() + ".LIB/" + request.name() + ".JRNRCV";
        String sql = """
                SELECT ASP_NUMBER, FILE_SYSTEM_IDENTIFIER,
                       FILE_IDENTIFIER_NUMBER, GENERATION_IDENTIFIER
                  FROM TABLE(QSYS2.IFS_OBJECT_STATISTICS(
                       START_PATH_NAME => ?, SUBTREE_DIRECTORIES => 'NO',
                       OBJECT_TYPE_LIST => '*JRNRCV', IGNORE_ERRORS => 'NO')) I
                 WHERE CAST(PATH_NAME AS VARCHAR(1024)) = ?
                 FETCH FIRST 2 ROWS ONLY
                """;
        String filesystem;
        String fileNumber;
        String generation;
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setQueryTimeout(ReadOnlyReceiverCatalog.catalogQueryTimeoutSeconds());
            statement.setString(1, path);
            statement.setString(2, path);
            try (ResultSet result = statement.executeQuery()) {
                if (!result.next()) {
                    throw new IllegalStateException("receiver object identity unavailable");
                }
                if (!integer(result.getBigDecimal("ASP_NUMBER")).equals(
                        BigInteger.valueOf(request.aspNumber()))) {
                    throw new IllegalStateException("receiver object ASP differs");
                }
                filesystem = integer(result.getBigDecimal("FILE_SYSTEM_IDENTIFIER")).toString();
                fileNumber = integer(result.getBigDecimal("FILE_IDENTIFIER_NUMBER")).toString();
                generation = integer(result.getBigDecimal("GENERATION_IDENTIFIER")).toString();
                if (result.next()) {
                    throw new IllegalStateException("receiver object identity ambiguous");
                }
            }
        }
        out.println("as400-receiver-identity-v1");
        out.printf("identity\t%s\t%s\t%s\t%s\t%d\t%s\t%s\t%s%n",
                journalLibrary, journalName, request.library(), request.name(),
                request.aspNumber(), filesystem, fileNumber, generation);
    }

    private static BigInteger integer(BigDecimal value) {
        if (value == null) {
            throw new IllegalStateException("receiver object identity incomplete");
        }
        BigInteger parsed = value.toBigIntegerExact();
        if (parsed.compareTo(MIN_ID) < 0 || parsed.compareTo(MAX_ID) > 0) {
            throw new IllegalStateException("receiver object identity outside BIGINT");
        }
        return parsed;
    }

    private static void identifier(String value) {
        if (value == null || !value.matches("[A-Z0-9_$#@]{1,10}")) {
            throw new IllegalArgumentException("invalid IBM i system identifier");
        }
    }
}
