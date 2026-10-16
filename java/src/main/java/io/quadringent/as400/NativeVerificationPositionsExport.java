package io.quadringent.as400;

import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/** Independent position SELECTs; receiver generations and historical formats require a controller. */
public final class NativeVerificationPositionsExport {
    private static final Pattern IDENTIFIER = Pattern.compile("[A-Z0-9_$#@]{1,10}");
    private static final Pattern HOST = Pattern.compile("[A-Za-z0-9][A-Za-z0-9.-]{0,252}");
    private static final Pattern USER = Pattern.compile("[A-Za-z0-9_$#@]{1,128}");
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern SEQUENCE = Pattern.compile("0|[1-9][0-9]{0,20}");
    private static final BigInteger MAX_SEQUENCE = BigInteger.TEN.pow(21).subtract(BigInteger.ONE);
    private static final long HARD_MAX_BYTES = 16L * 1024 * 1024 * 1024;
    private static final int MAX_SECONDS = 900;

    private NativeVerificationPositionsExport() { }

    public record Window(String journalLibrary, String journalName, String receiverLibrary, String receiverName,
                         String objectLibrary, String objectName, BigInteger start, BigInteger end,
                         String scopeSha256) {
        public Window {
            for (String value : new String[]{journalLibrary, journalName, receiverLibrary, receiverName,
                                             objectLibrary, objectName}) identifier(value);
            if (start == null || end == null || start.signum() < 1 || end.signum() < 0
                    || start.compareTo(MAX_SEQUENCE) > 0 || end.compareTo(MAX_SEQUENCE) > 0
                    || end.compareTo(start.subtract(BigInteger.ONE)) < 0 || scopeSha256 == null
                    || !SHA256.matcher(scopeSha256).matches()) {
                throw new IllegalArgumentException("invalid native position window");
            }
        }

        public boolean isEmpty() { return end.compareTo(start) < 0; }
    }

    public record Limits(long maxRows, long maxBytes, int maxLineBytes, int maxSeconds) {
        public Limits {
            if (maxRows < 1 || maxRows > 20_000_000 || maxBytes < 1 || maxBytes > HARD_MAX_BYTES
                    || maxLineBytes < 1 || maxLineBytes > 8 * 1024 * 1024
                    || maxSeconds < 1 || maxSeconds > MAX_SECONDS) {
                throw new IllegalArgumentException("invalid position export budget");
            }
        }

        public static Limits defaults() { return new Limits(2_000_000, 2L * 1024 * 1024 * 1024, 1024 * 1024, MAX_SECONDS); }
    }

    public record Summary(long rowCount, long byteCount, String sha256, String rowsSha256,
                          String startedAt, String completedAt) { }

    public static final class ExportFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private ExportFailure(String code) { super("native_positions_export_error=" + code); this.code = code; }
        public String code() { return code; }
    }

    @FunctionalInterface
    interface ConnectionFactory { Connection connect(String host, String user, String password) throws Exception; }

    public static void main(String[] args) {
        int status = run(args, System.in, System.out, System.err, NativeVerificationPositionsExport::connect);
        if (status != 0) System.exit(status);
    }

    static int run(String[] args, InputStream input, OutputStream output, OutputStream errors,
                   ConnectionFactory factory) {
        try {
            if (args == null || args.length != 13) throw new IllegalArgumentException("invalid arguments");
            Window window = new Window(args[0], args[1], args[2], args[3], args[4], args[5],
                    sequence(args[6]), sequence(args[7]), args[8]);
            Limits limits = new Limits(Long.parseLong(args[9]), Long.parseLong(args[10]),
                    Integer.parseInt(args[11]), Integer.parseInt(args[12]));
            Reader credentials = new InputStreamReader(input, StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT));
            String host = credentialLine(credentials, 253);
            String user = credentialLine(credentials, 128);
            String password = credentialLine(credentials, 4096);
            if (!HOST.matcher(host).matches() || !USER.matcher(user).matches() || password.isBlank()) {
                throw new IllegalArgumentException("invalid connection input");
            }
            requireTls();
            try (Connection connection = factory.connect(host, user, password)) {
                export(connection, window, limits, output);
            }
            return 0;
        } catch (ExportFailure failure) {
            return failure(errors, failure.code());
        } catch (IllegalArgumentException | IOException failure) {
            return failure(errors, "invalid_input");
        } catch (SQLException failure) {
            return failure(errors, "source_sql");
        } catch (Exception failure) {
            return failure(errors, "source_unavailable");
        }
    }

    private static int failure(OutputStream errors, String code) {
        try {
            errors.write(("native_positions_export_error=" + code + "\n").getBytes(StandardCharsets.US_ASCII));
            errors.flush();
        } catch (IOException ignored) { }
        return 2;
    }

    private static BigInteger sequence(String value) {
        if (value == null || !SEQUENCE.matcher(value).matches()) throw new IllegalArgumentException("invalid sequence");
        return new BigInteger(value);
    }

    private static void identifier(String value) {
        if (value == null || !IDENTIFIER.matcher(value).matches()) throw new IllegalArgumentException("invalid identifier");
    }

    private static String credentialLine(Reader input, int maximum) throws IOException {
        StringBuilder value = new StringBuilder();
        while (true) {
            int next = input.read();
            if (next < 0 || next == '\r' || next == 0) throw new IllegalArgumentException("invalid stdin input");
            if (next == '\n') return value.toString();
            if (value.length() == maximum) throw new IllegalArgumentException("stdin input exceeds budget");
            value.append((char) next);
        }
    }

    private static void requireTls() throws ExportFailure {
        for (String name : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String value = System.getProperty(name);
            if (value != null && Set.of("true", "1", "yes", "on").contains(value.trim().toLowerCase(Locale.ROOT))) {
                throw new ExportFailure("tls_required");
            }
        }
    }

    private static Connection connect(String host, String user, String password) throws Exception {
        requireTls();
        try { TlsTrust.install(true); }
        catch (RuntimeException failure) { throw new ExportFailure("tls_required"); }
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        DriverManager.setLoginTimeout(15);
        return DriverManager.getConnection("jdbc:as400://" + host,
                NativeVerificationSourceExport.connectionProperties(user, password));
    }

    /** Require successful return or CLI exit zero as well as the complete framed EOF. */
    public static Summary export(Connection connection, Window window, Limits limits, OutputStream output)
            throws ExportFailure {
        return export(connection, window, limits, output, System::nanoTime, Instant::now);
    }

    // Query/socket limits and an outer process deadline must bound blocking calls.
    static Summary export(Connection connection, Window window, Limits limits, OutputStream output,
                          LongSupplier monotonic, Supplier<Instant> utcNow) throws ExportFailure {
        try {
            if (connection == null || window == null || limits == null || output == null) {
                throw new ExportFailure("invalid_input");
            }
            Budget budget = new Budget(limits, monotonic);
            connection.setReadOnly(true);
            budget.check();
            Instant start = utc(utcNow);
            long count = 0;
            MessageDigest rowsHash = MessageDigest.getInstance("SHA-256");
            if (window.isEmpty()) {
                emit(output, header(window, start, limits), budget);
            } else {
                String sql = "SELECT RECEIVER_LIBRARY, RECEIVER_NAME, SEQUENCE_NUMBER" + from()
                        + " ORDER BY SEQUENCE_NUMBER";
                try (PreparedStatement statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY,
                        ResultSet.CONCUR_READ_ONLY)) {
                    bind(statement, window);
                    statement.setFetchSize(1000);
                    statement.setQueryTimeout(budget.querySeconds());
                    metadata(statement.getMetaData(), false);
                    try (ResultSet rows = statement.executeQuery()) {
                        budget.check();
                        metadata(rows.getMetaData(), false);
                        emit(output, header(window, start, limits), budget);
                        BigInteger previous = null;
                        while (true) {
                            budget.check();
                            boolean next = rows.next();
                            budget.check();
                            if (!next) break;
                            if (count == limits.maxRows()) throw new ExportFailure("row_budget_exceeded");
                            if (!window.receiverLibrary().equals(nativeName(rows.getString(1)))
                                    || !window.receiverName().equals(nativeName(rows.getString(2)))) {
                                throw new ExportFailure("receiver_mismatch");
                            }
                            BigInteger sequence = nativeSequence(rows.getBigDecimal(3));
                            budget.check();
                            if (sequence.compareTo(window.start()) < 0 || sequence.compareTo(window.end()) > 0
                                    || previous != null && sequence.compareTo(previous) < 0) {
                                throw new ExportFailure("position_mismatch");
                            }
                            byte[] row = frame("{\"kind\":\"row\",\"row\":{\"JOURNAL_RECEIVER\":\""
                                    + window.receiverName() + "\",\"JOURNAL_SEQUENCE\":\"" + sequence + "\"}}", limits);
                            emit(output, row, budget);
                            rowsHash.update(row);
                            count++;
                            previous = sequence;
                        }
                    }
                }
                budget.check();
                if (independentCount(connection, window, limits, budget) != count) {
                    throw new ExportFailure("position_count_mismatch");
                }
            }
            budget.check();
            Instant end = utc(utcNow);
            if (end.isBefore(start)) throw new ExportFailure("source_clock_reversed");
            String digest = HexFormat.of().formatHex(rowsHash.digest());
            emit(output, frame("{\"kind\":\"end\",\"row_count\":" + count + ",\"rows_sha256\":\"" + digest
                    + "\",\"completed_at\":\"" + end + "\"}", limits), budget);
            output.flush();
            if (output instanceof java.io.PrintStream print && print.checkError()) throw new IOException();
            budget.check();
            return new Summary(count, budget.bytes, HexFormat.of().formatHex(budget.hash.digest()), digest,
                    start.toString(), end.toString());
        } catch (ExportFailure failure) {
            throw failure;
        } catch (SQLException failure) {
            throw new ExportFailure("source_sql");
        } catch (IOException failure) {
            throw new ExportFailure("stream_io");
        } catch (Exception failure) {
            throw new ExportFailure("invalid_source");
        }
    }

    private static Instant utc(Supplier<Instant> clock) throws ExportFailure {
        Instant value = clock.get();
        if (value == null || value.atOffset(ZoneOffset.UTC).getYear() < 1
                || value.atOffset(ZoneOffset.UTC).getYear() > 9999) throw new ExportFailure("invalid_source_clock");
        return value;
    }

    private static byte[] header(Window window, Instant start, Limits limits) throws ExportFailure {
        return frame("{\"format_version\":\"quadringent-native-row-stream-v1\",\"kind\":\"header\","
                + "\"dataset\":\"positions\",\"scope_sha256\":\"" + window.scopeSha256()
                + "\",\"started_at\":\"" + start + "\"}", limits);
    }

    private static String from() {
        // Object arguments would select the current JID and hide older incarnations.
        return " FROM TABLE(QSYS2.DISPLAY_JOURNAL("
                + "JOURNAL_LIBRARY => ?, JOURNAL_NAME => ?, "
                + "STARTING_RECEIVER_LIBRARY => ?, STARTING_RECEIVER_NAME => ?, STARTING_SEQUENCE => ?, "
                + "ENDING_RECEIVER_LIBRARY => ?, ENDING_RECEIVER_NAME => ?, ENDING_SEQUENCE => ?, "
                + "JOURNAL_CODES => 'R', EOF_DELAY => 0)) "
                + "WHERE LEFT(OBJECT,20) = CAST(? AS CHAR(10)) CONCAT CAST(? AS CHAR(10)) "
                + "AND OBJECT_TYPE = '*FILE' AND JOURNAL_ENTRY_TYPE IN ('PT','PX','UB','UP','DL')";
    }

    private static void bind(PreparedStatement statement, Window window) throws SQLException {
        statement.setString(1, window.journalLibrary());
        statement.setString(2, window.journalName());
        statement.setString(3, window.receiverLibrary());
        statement.setString(4, window.receiverName());
        statement.setBigDecimal(5, new BigDecimal(window.start()));
        statement.setString(6, window.receiverLibrary());
        statement.setString(7, window.receiverName());
        statement.setBigDecimal(8, new BigDecimal(window.end()));
        statement.setString(9, window.objectName());
        statement.setString(10, window.objectLibrary());
    }

    private static long independentCount(Connection connection, Window window, Limits limits, Budget budget)
            throws Exception {
        // One overflow sentinel bounds this second scan; excess is refused, never admitted as a count.
        String sql = "SELECT COUNT(*) AS POSITION_COUNT FROM (SELECT SEQUENCE_NUMBER" + from()
                + " FETCH FIRST " + (limits.maxRows() + 1) + " ROWS ONLY) QDT_COUNT_BOUND";
        long count;
        try (PreparedStatement statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY,
                ResultSet.CONCUR_READ_ONLY)) {
            bind(statement, window);
            statement.setQueryTimeout(budget.querySeconds());
            metadata(statement.getMetaData(), true);
            try (ResultSet rows = statement.executeQuery()) {
                budget.check();
                metadata(rows.getMetaData(), true);
                if (!rows.next()) throw new ExportFailure("position_count_mismatch");
                BigDecimal number = rows.getBigDecimal(1);
                if (number == null || number.signum() < 0 || number.compareTo(BigDecimal.valueOf(limits.maxRows())) > 0) {
                    throw new ExportFailure("position_count_mismatch");
                }
                count = number.longValueExact();
                if (rows.next()) throw new ExportFailure("position_count_mismatch");
            }
        }
        budget.check();
        return count;
    }

    private static void metadata(ResultSetMetaData metadata, boolean count) throws Exception {
        if (metadata == null || metadata.getColumnCount() != (count ? 1 : 3)) throw new ExportFailure("metadata_mismatch");
        String[] names = count ? new String[]{"POSITION_COUNT"}
                : new String[]{"RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER"};
        for (int index = 0; index < names.length; index++) {
            int column = index + 1;
            if (!names[index].equalsIgnoreCase(metadata.getColumnLabel(column))) throw new ExportFailure("metadata_mismatch");
            int type = metadata.getColumnType(column);
            int precision = metadata.getPrecision(column);
            if (!count && column < 3) {
                if ((type != Types.CHAR && type != Types.VARCHAR) || precision < 1 || precision > 10) {
                    throw new ExportFailure("metadata_mismatch");
                }
            } else if (count) {
                if (!Set.of(Types.BIGINT, Types.INTEGER, Types.DECIMAL, Types.NUMERIC).contains(type)
                        || precision < 1 || precision > 21 || metadata.getScale(column) != 0) {
                    throw new ExportFailure("metadata_mismatch");
                }
            } else if ((type != Types.DECIMAL && type != Types.NUMERIC) || precision != 21 || metadata.getScale(column) != 0) {
                throw new ExportFailure("metadata_mismatch");
            }
        }
    }

    private static String nativeName(String value) throws ExportFailure {
        if (value == null || value.length() > 10) throw new ExportFailure("receiver_mismatch");
        int end = value.length();
        while (end > 0 && value.charAt(end - 1) == ' ') end--;
        String name = value.substring(0, end);
        if (!IDENTIFIER.matcher(name).matches()) throw new ExportFailure("receiver_mismatch");
        return name;
    }

    private static BigInteger nativeSequence(BigDecimal number) throws ExportFailure {
        if (number == null || number.signum() < 0 || number.scale() > 21 || number.precision() > 21
                || (long) number.precision() - Math.min(0L, number.scale()) > 21) {
            throw new ExportFailure("position_mismatch");
        }
        BigInteger sequence = number.toBigIntegerExact();
        if (sequence.compareTo(MAX_SEQUENCE) > 0) throw new ExportFailure("position_mismatch");
        return sequence;
    }

    private static byte[] frame(String value, Limits limits) throws ExportFailure {
        if (value.length() + 1 > limits.maxLineBytes()) throw new ExportFailure("line_budget_exceeded");
        return (value + "\n").getBytes(StandardCharsets.US_ASCII);
    }

    private static void emit(OutputStream output, byte[] bytes, Budget budget) throws Exception {
        budget.check();
        if (bytes.length > budget.limits.maxBytes() - budget.bytes) throw new ExportFailure("byte_budget_exceeded");
        output.write(bytes);
        budget.bytes += bytes.length;
        budget.hash.update(bytes);
        budget.check();
    }

    private static final class Budget {
        final Limits limits;
        final LongSupplier monotonic;
        final long started;
        final MessageDigest hash;
        long previous, bytes;
        Budget(Limits limits, LongSupplier monotonic) throws Exception {
            this.limits = limits; this.monotonic = monotonic;
            started = previous = monotonic.getAsLong();
            hash = MessageDigest.getInstance("SHA-256");
        }
        void check() throws ExportFailure {
            long now = monotonic.getAsLong();
            if (now < previous || now - started > limits.maxSeconds() * 1_000_000_000L) {
                throw new ExportFailure("time_budget_exceeded");
            }
            previous = now;
        }
        int querySeconds() throws ExportFailure {
            check();
            long remaining = limits.maxSeconds() * 1_000_000_000L - (previous - started);
            return (int) Math.max(1, Math.min(MAX_SECONDS, (remaining + 999_999_999L) / 1_000_000_000L));
        }
    }
}
