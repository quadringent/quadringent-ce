package io.quadringent.as400;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.math.BigDecimal;
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
import java.time.LocalDate;
import java.time.LocalTime;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Properties;
import java.util.Set;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/** Read-only SOURCE frames. A matching COUNT does not establish a stable source cut. */
public final class NativeVerificationSourceExport {
    private static final Pattern IDENTIFIER = Pattern.compile("[A-Za-z_][A-Za-z0-9_]{0,127}");
    private static final Pattern HOST = Pattern.compile("[A-Za-z0-9][A-Za-z0-9.-]{0,252}");
    private static final Pattern USER = Pattern.compile("[A-Za-z0-9_$#@]{1,128}");
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern TIMESTAMP = Pattern.compile(
            "[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,12})?");
    private static final int MAX_NUMBER_CHARS = 1024;
    private static final int QUERY_TIMEOUT_SECONDS = 900;
    private static final long HARD_MAX_BYTES = 16L * 1024 * 1024 * 1024;

    private NativeVerificationSourceExport() { }

    public record Scope(String library, String table, String journalLibrary, String journalName,
                        List<String> primaryKeys, List<String> columns, String scopeSha256) {
        public Scope {
            for (String value : new String[]{library, table, journalLibrary, journalName}) identifier(value);
            columns = identifiers(columns, 512);
            primaryKeys = identifiers(primaryKeys, 512);
            if (!columns.containsAll(primaryKeys) || scopeSha256 == null || !SHA256.matcher(scopeSha256).matches()) {
                throw new IllegalArgumentException("invalid source export scope");
            }
        }
        String qualifiedTable() { return library + "." + table; }
    }

    public record Limits(long maxRows, long maxBytes, int maxLineBytes, int maxCellChars, int maxSeconds) {
        public Limits {
            if (maxRows < 1 || maxRows > 20_000_000 || maxBytes < 1 || maxBytes > HARD_MAX_BYTES
                    || maxLineBytes < 1 || maxLineBytes > 8 * 1024 * 1024
                    || maxCellChars < 1 || maxCellChars > 1024 * 1024
                    || maxSeconds < 1 || maxSeconds > QUERY_TIMEOUT_SECONDS) {
                throw new IllegalArgumentException("invalid source export budget");
            }
        }
        public static Limits defaults() {
            return new Limits(20_000_000, HARD_MAX_BYTES, 8 * 1024 * 1024, 1024 * 1024, QUERY_TIMEOUT_SECONDS);
        }
    }

    public record Summary(long rowCount, long byteCount, String rowsSha256) { }

    public static final class ExportFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private ExportFailure(String code) { super("native_source_export_error=" + code); this.code = code; }
        public String code() { return code; }
    }

    @FunctionalInterface
    interface ConnectionFactory { Connection connect(String host, String user, String password) throws Exception; }

    public static void main(String[] args) {
        int status = run(args, System.in, System.out, System.err, NativeVerificationSourceExport::connect);
        if (status != 0) System.exit(status);
    }

    static int run(String[] args, InputStream input, OutputStream output, OutputStream errors,
                   ConnectionFactory factory) {
        try {
            if (args == null || args.length != 9) throw new IllegalArgumentException("invalid arguments");
            Scope scope = new Scope(args[0], args[1], args[2], args[3],
                    List.of(args[4].split(",", -1)), List.of(args[5].split(",", -1)), args[6]);
            Limits defaults = Limits.defaults();
            Limits limits = new Limits(Long.parseLong(args[7]), Long.parseLong(args[8]),
                    defaults.maxLineBytes(), defaults.maxCellChars(), defaults.maxSeconds());
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
                export(connection, scope, limits, output);
            }
            return 0;
        } catch (ExportFailure failure) {
            return failure(errors, failure.code());
        } catch (IllegalArgumentException failure) {
            return failure(errors, "invalid_input");
        } catch (SQLException failure) {
            return failure(errors, "source_sql");
        } catch (Exception failure) {
            return failure(errors, "source_unavailable");
        }
    }

    private static int failure(OutputStream errors, String code) {
        try {
            errors.write(("native_source_export_error=" + code + "\n").getBytes(StandardCharsets.US_ASCII));
            errors.flush();
        } catch (IOException ignored) { }
        return 2;
    }

    private static String credentialLine(Reader reader, int maximum) throws IOException {
        StringBuilder value = new StringBuilder();
        while (true) {
            int next = reader.read();
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

    static Properties connectionProperties(String user, String password) {
        Properties properties = new Properties();
        properties.setProperty("user", user);
        properties.setProperty("password", password);
        properties.setProperty("secure", "true");
        properties.setProperty("access", "read only");
        properties.setProperty("prompt", "false");
        properties.setProperty("date format", "iso");
        properties.setProperty("socket timeout", "120000");
        properties.setProperty("login timeout", "15");
        return properties;
    }

    private static Connection connect(String host, String user, String password) throws Exception {
        requireTls();
        try { TlsTrust.install(true); }
        catch (RuntimeException error) { throw new ExportFailure("tls_required"); }
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        DriverManager.setLoginTimeout(15);
        return DriverManager.getConnection("jdbc:as400://" + host, connectionProperties(user, password));
    }

    /** Completion requires a successful return as well as the framed EOF. */
    public static Summary export(Connection connection, Scope scope, Limits limits, OutputStream output)
            throws ExportFailure {
        return export(connection, scope, limits, output, System::nanoTime, Instant::now);
    }

    // Blocking streams need their own timeout; JDBC also has socket and query limits.
    static Summary export(Connection connection, Scope scope, Limits limits, OutputStream output,
                          LongSupplier monotonic, Supplier<Instant> utcNow) throws ExportFailure {
        try {
            Budget budget = new Budget(limits, monotonic);
            connection.setReadOnly(true);
            checkJournal(connection, scope, budget);
            MessageDigest rowsHash = MessageDigest.getInstance("SHA-256");
            String start = utcNow.get().toString();
            long count = 0;
            String sql = "SELECT " + String.join(",", scope.columns()) + " FROM " + scope.qualifiedTable()
                    + " ORDER BY " + String.join(",", scope.primaryKeys());
            int[] types;
            try (PreparedStatement description = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY,
                    ResultSet.CONCUR_READ_ONLY)) {
                description.setQueryTimeout(budget.querySeconds());
                types = admitMetadata(description.getMetaData(), scope, null);
            }
            Projection projection = projection(scope, limits, types, sql);
            try (PreparedStatement statement = connection.prepareStatement(projection.sql(), ResultSet.TYPE_FORWARD_ONLY,
                    ResultSet.CONCUR_READ_ONLY)) {
                statement.setFetchSize(1000);
                statement.setQueryTimeout(budget.querySeconds());
                admitProjection(statement.getMetaData(), scope, projection, types);
                try (ResultSet rows = statement.executeQuery()) {
                    budget.check();
                    admitProjection(rows.getMetaData(), scope, projection, types);
                    emit(output, frame(limits, "{\"format_version\":\"quadringent-native-row-stream-v1\","
                            + "\"kind\":\"header\",\"dataset\":\"source\",\"scope_sha256\":\""
                            + scope.scopeSha256() + "\",\"started_at\":\"" + start + "\"}"), budget);
                    while (true) {
                        budget.check();
                        boolean next = rows.next();
                        budget.check();
                        if (!next) break;
                        if (count == limits.maxRows()) throw new ExportFailure("row_budget_exceeded");
                        if (projection.flag() != null) {
                            BigDecimal safe = rows.getBigDecimal(types.length + 1);
                            if (safe == null || safe.compareTo(BigDecimal.ZERO) != 0) {
                                throw new ExportFailure("projection_budget_exceeded");
                            }
                        }
                        Frame row = new Frame(limits.maxLineBytes());
                        row.ascii("{\"kind\":\"row\",\"row\":{");
                        for (int index = 0; index < types.length; index++) {
                            if (index != 0) row.ascii(",");
                            row.string(scope.columns().get(index));
                            row.ascii(":");
                            String value = value(rows, index + 1, types[index], limits, budget);
                            if (value == null) row.ascii("null"); else row.string(value);
                        }
                        row.ascii("}}");
                        byte[] data = row.finish();
                        emit(output, data, budget);
                        rowsHash.update(data);
                        count++;
                    }
                }
            }
            if (count(connection, scope, budget) != count) throw new ExportFailure("source_count_mismatch");
            budget.check();
            Instant completed = utcNow.get();
            if (completed.isBefore(Instant.parse(start))) throw new ExportFailure("source_clock_reversed");
            String digest = HexFormat.of().formatHex(rowsHash.digest());
            byte[] footer = frame(limits, "{\"kind\":\"end\",\"row_count\":" + count
                    + ",\"rows_sha256\":\"" + digest + "\",\"completed_at\":\"" + completed + "\"}");
            budget.check();
            emit(output, footer, budget);
            output.flush();
            if (output instanceof java.io.PrintStream print && print.checkError()) throw new IOException();
            budget.check();
            return new Summary(count, budget.bytes, digest);
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

    private static void checkJournal(Connection connection, Scope scope, Budget budget) throws Exception {
        String sql = "SELECT JOURNAL_LIBRARY, JOURNAL_NAME FROM QSYS2.JOURNALED_OBJECTS "
                + "WHERE OBJECT_LIBRARY = ? AND OBJECT_NAME = ? AND OBJECT_TYPE = '*FILE' FETCH FIRST 2 ROWS ONLY";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, scope.library().toUpperCase(Locale.ROOT));
            statement.setString(2, scope.table().toUpperCase(Locale.ROOT));
            statement.setQueryTimeout(budget.querySeconds());
            try (ResultSet rows = statement.executeQuery()) {
                budget.check();
                if (!rows.next()) throw new ExportFailure("journal_mismatch");
                String library = text(rows.getCharacterStream(1), 128, budget);
                String name = text(rows.getCharacterStream(2), 128, budget);
                if (library == null || name == null || !scope.journalLibrary().equalsIgnoreCase(library.trim())
                        || !scope.journalName().equalsIgnoreCase(name.trim()) || rows.next()) {
                    throw new ExportFailure("journal_mismatch");
                }
                budget.check();
            }
        }
        budget.check();
    }

    private record Projection(String sql, String flag) { }

    private static boolean isText(int type) {
        return switch (type) {
            case Types.CHAR, Types.VARCHAR, Types.LONGVARCHAR, Types.NCHAR, Types.NVARCHAR,
                 Types.LONGNVARCHAR, Types.CLOB, Types.NCLOB -> true;
            default -> false;
        };
    }

    private static String internalName(Scope scope, String prefix) {
        Set<String> names = new HashSet<>();
        for (String column : scope.columns()) names.add(column.toUpperCase(Locale.ROOT));
        int suffix = 0;
        while (names.contains(prefix + suffix)) suffix++;
        return prefix + suffix;
    }

    private static Projection projection(Scope scope, Limits limits, int[] types, String original) {
        StringBuilder guards = new StringBuilder();
        StringBuilder total = new StringBuilder();
        for (int i = 0; i < types.length; i++) {
            if (!isText(types[i])) continue;
            String size = "COALESCE(BIGINT(OCTET_LENGTH(" + scope.columns().get(i) + ")),0)";
            if (!guards.isEmpty()) { guards.append(" AND "); total.append('+'); }
            guards.append(size).append(" <= ").append(4L * limits.maxCellChars());
            total.append(size);
        }
        if (guards.isEmpty()) return new Projection(original, null);
        // SQL-side ceilings bound inline conversion before any application Reader exists.
        guards.append(" AND (").append(total).append(") <= ").append(4L * limits.maxLineBytes());
        String safe = internalName(scope, "QDT_SOURCE_SAFE_");
        String flag = internalName(scope, "QDT_SOURCE_OVERSIZE_");
        StringBuilder select = new StringBuilder("SELECT ");
        for (int i = 0; i < types.length; i++) {
            if (i != 0) select.append(',');
            String column = scope.columns().get(i);
            if (isText(types[i])) {
                select.append("CASE WHEN ").append(safe).append("=1 THEN ").append(column)
                        .append(" ELSE NULL END AS ").append(column);
            } else select.append(column);
        }
        select.append(",CASE WHEN ").append(safe).append("=1 THEN 0 ELSE 1 END AS ").append(flag)
                .append(" FROM (SELECT ").append(String.join(",", scope.columns()))
                .append(",CASE WHEN ").append(guards).append(" THEN 1 ELSE 0 END AS ").append(safe)
                .append(" FROM ").append(scope.qualifiedTable()).append(") QDT_SOURCE_INPUT ORDER BY ")
                .append(String.join(",", scope.primaryKeys()));
        return new Projection(select.toString(), flag);
    }

    private static void admitProjection(ResultSetMetaData metadata, Scope scope, Projection projection, int[] expected)
            throws Exception {
        int[] actual = admitMetadata(metadata, scope, projection.flag());
        for (int i = 0; i < actual.length; i++) {
            if (actual[i] != expected[i] && !(isText(actual[i]) && isText(expected[i]))) {
                throw new ExportFailure("column_mismatch");
            }
        }
    }

    private static int[] admitMetadata(ResultSetMetaData metadata, Scope scope, String flag) throws Exception {
        int count = scope.columns().size();
        if (metadata == null || metadata.getColumnCount() != count + (flag == null ? 0 : 1)) {
            throw new ExportFailure("column_mismatch");
        }
        if (flag != null && (!flag.equalsIgnoreCase(metadata.getColumnLabel(count + 1))
                || metadata.getColumnType(count + 1) != Types.INTEGER)) {
            throw new ExportFailure("column_mismatch");
        }
        int[] types = new int[scope.columns().size()];
        for (int index = 0; index < types.length; index++) {
            int column = index + 1;
            if (!scope.columns().get(index).equalsIgnoreCase(metadata.getColumnLabel(column))) {
                throw new ExportFailure("column_mismatch");
            }
            int type = metadata.getColumnType(column);
            switch (type) {
                case Types.TINYINT, Types.SMALLINT, Types.INTEGER, Types.BIGINT,
                     Types.DECIMAL, Types.NUMERIC -> {
                    if (metadata.getPrecision(column) < 1 || metadata.getPrecision(column) > MAX_NUMBER_CHARS) {
                        throw new ExportFailure("unsupported_type");
                    }
                }
                case Types.CHAR, Types.VARCHAR, Types.LONGVARCHAR, Types.NCHAR, Types.NVARCHAR,
                     Types.LONGNVARCHAR, Types.CLOB, Types.NCLOB, Types.DATE -> { }
                case Types.TIMESTAMP -> {
                    if (metadata.getScale(column) < 0 || metadata.getScale(column) > 12) {
                        throw new ExportFailure("unsupported_type");
                    }
                }
                default -> throw new ExportFailure("unsupported_type");
            }
            types[index] = type;
        }
        return types;
    }

    private static String value(ResultSet rows, int column, int type, Limits limits, Budget budget) throws Exception {
        budget.check();
        String result;
        switch (type) {
            case Types.TINYINT, Types.SMALLINT, Types.INTEGER, Types.BIGINT, Types.DECIMAL, Types.NUMERIC -> {
                BigDecimal number = rows.getBigDecimal(column);
                if (number == null) return null;
                long expanded = (long) number.precision() + Math.abs((long) number.scale()) + 2;
                if (expanded > MAX_NUMBER_CHARS) throw new ExportFailure("cell_budget_exceeded");
                if (type != Types.DECIMAL && type != Types.NUMERIC) number.toBigIntegerExact();
                result = number.signum() == 0 ? "0" : number.toPlainString();
            }
            case Types.DATE -> {
                java.sql.Date date = rows.getDate(column);
                result = date == null ? null : date.toLocalDate().toString();
            }
            default -> {
                result = text(rows.getCharacterStream(column), limits.maxCellChars(), budget);
                if (result != null && type == Types.TIMESTAMP) {
                    if (!TIMESTAMP.matcher(result).matches()) throw new ExportFailure("invalid_timestamp");
                    LocalDate day = LocalDate.parse(result.substring(0, 10));
                    if (day.getYear() < 1) throw new ExportFailure("invalid_timestamp");
                    LocalTime.of(Integer.parseInt(result.substring(11, 13)), Integer.parseInt(result.substring(14, 16)),
                            Integer.parseInt(result.substring(17, 19)));
                    result = result.replace(' ', 'T');
                }
            }
        }
        budget.check();
        if (result != null && result.length() > limits.maxCellChars()) throw new ExportFailure("cell_budget_exceeded");
        return result;
    }

    private static String text(Reader reader, int limit, Budget budget) throws Exception {
        if (reader == null) return null;
        try (reader) {
            StringBuilder value = new StringBuilder(Math.min(limit, 1024));
            char[] chunk = new char[Math.min(limit + 1, 2048)];
            while (true) {
                budget.check();
                int count = reader.read(chunk, 0, Math.min(chunk.length, limit - value.length() + 1));
                budget.check();
                if (count < 0) return value.toString();
                if (count == 0) throw new ExportFailure("invalid_text_stream");
                if (count > limit - value.length()) throw new ExportFailure("cell_budget_exceeded");
                value.append(chunk, 0, count);
            }
        }
    }

    private static long count(Connection connection, Scope scope, Budget budget) throws Exception {
        long result;
        try (PreparedStatement statement = connection.prepareStatement("SELECT COUNT(*) FROM " + scope.qualifiedTable())) {
            statement.setQueryTimeout(budget.querySeconds());
            try (ResultSet rows = statement.executeQuery()) {
                budget.check();
                if (!rows.next()) throw new ExportFailure("source_count_mismatch");
                BigDecimal number = rows.getBigDecimal(1);
                if (number == null || number.signum() < 0 || number.precision() > 20) {
                    throw new ExportFailure("source_count_mismatch");
                }
                result = number.longValueExact();
                if (rows.next()) throw new ExportFailure("source_count_mismatch");
            }
        }
        budget.check();
        return result;
    }

    private static void identifier(String name) {
        if (name == null || !IDENTIFIER.matcher(name).matches()) throw new IllegalArgumentException("invalid identifier");
    }

    private static List<String> identifiers(List<String> names, int maximum) {
        if (names == null || names.isEmpty() || names.size() > maximum) throw new IllegalArgumentException("invalid columns");
        Set<String> unique = new HashSet<>();
        for (String name : names) {
            identifier(name);
            if (!unique.add(name.toUpperCase(Locale.ROOT))) throw new IllegalArgumentException("duplicate identifier");
        }
        return List.copyOf(names);
    }

    private static byte[] frame(Limits limits, String ascii) throws ExportFailure {
        Frame frame = new Frame(limits.maxLineBytes());
        frame.ascii(ascii);
        return frame.finish();
    }

    private static void emit(OutputStream output, byte[] data, Budget budget) throws Exception {
        budget.check();
        if (data.length > budget.limits.maxBytes() - budget.bytes) throw new ExportFailure("byte_budget_exceeded");
        output.write(data, 0, data.length);
        budget.bytes += data.length;
        budget.check();
    }

    private static final class Budget {
        final Limits limits;
        final LongSupplier monotonic;
        final long started;
        long previous, bytes;
        Budget(Limits limits, LongSupplier monotonic) {
            this.limits = limits; this.monotonic = monotonic;
            started = previous = monotonic.getAsLong();
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
            return (int) Math.max(1, Math.min(QUERY_TIMEOUT_SECONDS, (remaining + 999_999_999L) / 1_000_000_000L));
        }
    }

    private static final class Frame {
        final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        final int maximum;
        Frame(int maximum) { this.maximum = maximum; }
        void one(int value) throws ExportFailure {
            if (bytes.size() >= maximum - 1) throw new ExportFailure("line_budget_exceeded");
            bytes.write(value);
        }
        void ascii(String value) throws ExportFailure {
            for (int i = 0; i < value.length(); i++) one(value.charAt(i));
        }
        void string(String value) throws ExportFailure {
            one('"');
            for (int i = 0; i < value.length(); i++) {
                char c = value.charAt(i);
                switch (c) {
                    case '"' -> ascii("\\\"");
                    case '\\' -> ascii("\\\\");
                    case '\b' -> ascii("\\b");
                    case '\f' -> ascii("\\f");
                    case '\n' -> ascii("\\n");
                    case '\r' -> ascii("\\r");
                    case '\t' -> ascii("\\t");
                    default -> {
                        if (c < 0x20) {
                            ascii("\\u00"); one(Character.forDigit(c >>> 4, 16)); one(Character.forDigit(c & 15, 16));
                        } else if (Character.isHighSurrogate(c)) {
                            if (i + 1 == value.length() || !Character.isLowSurrogate(value.charAt(i + 1))) {
                                throw new ExportFailure("invalid_unicode");
                            }
                            int point = Character.toCodePoint(c, value.charAt(++i));
                            one(0xf0 | point >>> 18); one(0x80 | (point >>> 12 & 63));
                            one(0x80 | (point >>> 6 & 63)); one(0x80 | (point & 63));
                        } else if (Character.isLowSurrogate(c)) {
                            throw new ExportFailure("invalid_unicode");
                        } else if (c < 0x80) one(c);
                        else if (c < 0x800) { one(0xc0 | c >>> 6); one(0x80 | c & 63); }
                        else { one(0xe0 | c >>> 12); one(0x80 | c >>> 6 & 63); one(0x80 | c & 63); }
                    }
                }
            }
            one('"');
        }
        byte[] finish() throws ExportFailure {
            if (bytes.size() >= maximum) throw new ExportFailure("line_budget_exceeded");
            bytes.write('\n');
            return bytes.toByteArray();
        }
    }
}
