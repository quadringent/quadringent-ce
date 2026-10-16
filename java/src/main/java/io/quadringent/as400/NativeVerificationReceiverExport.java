package io.quadringent.as400;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.Reader;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Arrays;
import java.util.Base64;
import java.util.List;
import java.util.Locale;
import java.util.Properties;
import java.util.Set;

/** Read-only native originals; matching sampled identities establish no stable cut. */
public final class NativeVerificationReceiverExport {
    private static final int MAX_CATALOG_BYTES = 256 * 1024;

    private NativeVerificationReceiverExport() { }

    record Scope(String journalLibrary, String journalName, ReadOnlyReceiverIdentity.Request receiver,
                 String sha256, String phase) {
        Scope {
            new ReadOnlyReceiverIdentity.Request(journalLibrary, journalName, 1);
            if (receiver == null || sha256 == null || !sha256.matches("[0-9a-f]{64}")
                    || !Set.of("before", "after").contains(phase)) {
                throw new IllegalArgumentException("invalid receiver scope");
            }
        }
    }

    @FunctionalInterface
    interface ConnectionFactory {
        Connection connect(String host, String user, String password) throws Exception;
    }

    public static void main(String[] args) {
        int status = run(args, System.in, System.out, System.err, NativeVerificationReceiverExport::connect);
        if (status != 0) System.exit(status);
    }

    static int run(String[] args, InputStream input, OutputStream output, OutputStream errors,
                   ConnectionFactory factory) {
        try {
            if (args == null || args.length != 7 || !"1".equals(args[4])) {
                throw new IllegalArgumentException("invalid receiver arguments");
            }
            Scope scope = new Scope(args[0], args[1], new ReadOnlyReceiverIdentity.Request(args[2], args[3], 1),
                                    args[5], args[6]);
            Reader reader = new InputStreamReader(input, StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT));
            String host = line(reader, 253);
            String user = line(reader, 128);
            String password = line(reader, 4096);
            if (!host.matches("[A-Za-z0-9][A-Za-z0-9.-]{0,252}") || !user.matches("[A-Za-z0-9_$#@]{1,128}")
                    || password.isBlank() || reader.read() != -1) {
                throw new IllegalArgumentException("invalid receiver connection input");
            }
            requireTls();
            try (Connection connection = factory.connect(host, user, password)) {
                export(connection, scope, output);
            }
            return 0;
        } catch (IllegalArgumentException error) {
            return failure(errors, "invalid_input");
        } catch (SQLException error) {
            return failure(errors, "receiver_sql");
        } catch (Exception error) {
            return failure(errors, "receiver_unavailable");
        }
    }

    private static String line(Reader reader, int maximum) throws IOException {
        StringBuilder result = new StringBuilder();
        while (true) {
            int next = reader.read();
            if (next < 0 || next == '\r' || next == 0) throw new IllegalArgumentException("invalid stdin input");
            if (next == '\n') return result.toString();
            if (result.length() == maximum) throw new IllegalArgumentException("stdin budget exceeded");
            result.append((char) next);
        }
    }

    private static void requireTls() {
        for (String name : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String value = System.getProperty(name);
            if (value != null && Set.of("true", "1", "yes", "on").contains(value.trim().toLowerCase(Locale.ROOT))) {
                throw new IllegalArgumentException("TLS verification required");
            }
        }
    }

    static Properties connectionProperties(String user, String password) {
        Properties properties = new Properties();
        properties.setProperty("user", user);
        properties.setProperty("password", password);
        properties.setProperty("secure", "true");
        properties.setProperty("access", "read only");
        properties.setProperty("prompt", "false");
        properties.setProperty("login timeout", "15");
        properties.setProperty("socket timeout", "30000");
        return properties;
    }

    private static Connection connect(String host, String user, String password) throws Exception {
        requireTls();
        TlsTrust.install(true);
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        DriverManager.setLoginTimeout(15);
        return DriverManager.getConnection("jdbc:as400://" + host, connectionProperties(user, password));
    }

    static void export(Connection connection, Scope scope, OutputStream output) throws Exception {
        connection.setReadOnly(true);
        byte[] before = capture(256, out -> ReadOnlyReceiverIdentity.writeIdentity(connection, scope.journalLibrary(),
                scope.journalName(), scope.receiver(), out));
        byte[] catalog = capture(MAX_CATALOG_BYTES, out -> ReadOnlyReceiverCatalog.writeReceivers(connection,
                scope.journalLibrary(), scope.journalName(), 100, scope.receiver().name(), out));
        byte[] after = capture(256, out -> ReadOnlyReceiverIdentity.writeIdentity(connection, scope.journalLibrary(),
                scope.journalName(), scope.receiver(), out));
        if (!Arrays.equals(before, after)) throw new IllegalStateException("receiver identity changed");
        String document = "{\"format_version\":\"quadringent-native-receiver-observation-v1\","
                + "\"scope_sha256\":\"" + scope.sha256() + "\",\"phase\":\"" + scope.phase() + "\","
                + "\"identity_before_base64\":\"" + Base64.getEncoder().encodeToString(before) + "\","
                + "\"catalog_base64\":\"" + Base64.getEncoder().encodeToString(catalog) + "\","
                + "\"identity_after_base64\":\"" + Base64.getEncoder().encodeToString(after) + "\"}\n";
        output.write(document.getBytes(StandardCharsets.US_ASCII));
        output.flush();
    }

    @FunctionalInterface
    private interface NativeWriter {
        void write(PrintStream output) throws Exception;
    }

    private static byte[] capture(int maximum, NativeWriter writer) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        OutputStream bounded = new OutputStream() {
            @Override
            public void write(int value) throws IOException {
                if (bytes.size() == maximum) throw new IOException("native receiver byte budget");
                bytes.write(value);
            }

            @Override
            public void write(byte[] value, int start, int count) throws IOException {
                if (count > maximum - bytes.size()) throw new IOException("native receiver byte budget");
                bytes.write(value, start, count);
            }
        };
        try (PrintStream out = new PrintStream(bounded, false, StandardCharsets.UTF_8)) {
            writer.write(out);
            out.flush();
            if (out.checkError()) throw new IOException("native receiver original incomplete");
        }
        return bytes.toByteArray();
    }

    private static int failure(OutputStream errors, String code) {
        try {
            errors.write(("native_receiver_export_error=" + code + "\n").getBytes(StandardCharsets.US_ASCII));
            errors.flush();
        } catch (IOException ignored) { }
        return 2;
    }
}
