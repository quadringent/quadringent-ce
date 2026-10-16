package io.quadringent.as400;

import java.io.InputStream;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Types;
import java.util.Base64;
import java.util.HexFormat;
import java.util.Set;
import static io.quadringent.as400.NativeCopyObservationExport.q;

/** Scoped R/D/F coverage through a real forced prefix; never global native admission. */
public final class NativeLifecycleGuard {
    private static final int MAX_METADATA = 1048576;
    private static final String FROM = " FROM TABLE(QSYS2.DISPLAY_JOURNAL(JOURNAL_LIBRARY => ?,JOURNAL_NAME => ?,"
        + "STARTING_RECEIVER_LIBRARY => ?,STARTING_RECEIVER_NAME => ?,STARTING_SEQUENCE => ?," + "ENDING_RECEIVER_LIBRARY => ?,ENDING_RECEIVER_NAME => ?,ENDING_SEQUENCE => ?,JOURNAL_CODES => "
        + "'D,F,R',EOF_DELAY => 0)) WHERE (JOURNAL_CODE IN ('D','F') OR (JOURNAL_CODE = 'R' AND " + "LEFT(OBJECT,20) = CAST(? AS CHAR(10)) CONCAT CAST(? AS CHAR(10))))";
    private static final String SQL = "SELECT RECEIVER_LIBRARY,RECEIVER_NAME,SEQUENCE_NUMBER,JOURNAL_CODE,JOURNAL_ENTRY_TYPE,OBJECT,"
        + "OBJECT_TYPE,CHAR(HEX(JOURNAL_IDENTIFIER),20) AS JID_HEX,MINIMIZED_ENTRY_DATA," + "MINIMIZED_ON_FIELD_BOUNDARY,INDICATOR_FLAG,NULL_VALUE_INDICATORS,"
        + "BIGINT(OCTET_LENGTH(ENTRY_DATA)) AS PAYLOAD_LENGTH,CASE WHEN OCTET_LENGTH(ENTRY_DATA) <= 65536 " + "THEN CAST(ENTRY_DATA AS BLOB(65536)) ELSE CAST(NULL AS BLOB(65536)) END AS ENTRY_DATA";
    private NativeLifecycleGuard() {
    }
    public static void main(String[] args) {
        int status = NativeBusinessImageProducer.run(args, System.in, System.out, System.err,
            NativeCopyObservationExport.NativeSession::new, true);
        if (status != 0) {
            System.exit(status);
        }
    }
    public static final class GuardFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private final String partial;
        private GuardFailure(String code, String partial) {
            super("native_lifecycle_guard_error=" + code);
            this.code = code;
            this.partial = partial;
        }
        public String code() {
            return code;
        }
        public String partialJson() {
            return partial;
        }
    }
    public static final class Proof {
        private final NativeBusinessImageExport.Request request;
        private final String layoutSha;
        private final BigInteger boundary;
        private final NativeJournalPrefixForce.Result prefix;
        private final String json;
        private Proof(NativeBusinessImageExport.Request request, String layoutSha, BigInteger boundary,
            NativeJournalPrefixForce.Result prefix, String json) {
            this.request = request;
            this.layoutSha = layoutSha;
            this.boundary = boundary;
            this.prefix = prefix;
            this.json = json;
        }
        public String json() {
            return json;
        }
        public NativeJournalPrefixForce.Result prefix() {
            return prefix;
        }
        boolean matches(NativeBusinessImageExport.Request scope, NativeBusinessImageExport.Layout layout) {
            return request.equals(scope) && layoutSha.equals(layout.sha256());
        }
    }
    public static Proof scan(Connection connection, NativeBusinessImageExport.Request request,
        NativeBusinessImageExport.Layout layout, BigInteger boundary, NativeJournalPrefixForce.Result prefix, long maxRows,
        int seconds) throws GuardFailure {
        StringBuilder entries = new StringBuilder();
        String prefixJson = "null";
        try {
            if (connection == null || request == null || layout == null || boundary == null || prefix == null || boundary.signum() < 1
                || maxRows < 1 || maxRows > 20000000 || seconds < 1 || seconds > 60) {
                throw new IllegalArgumentException();
            }
            prefixJson = prefix.json();
            var end = new BigInteger(prefix.prefix().sequence());
            if (!prefix.target().journalLibrary().equals(request.journalLibrary()) || !prefix.target().journalName().equals(request.journalName())
                || !prefix.prefix().receiverLibrary().equals(request.receiverLibrary()) || !prefix.prefix().receiverName().equals(request.receiverName())
                || !request.start().equals(boundary.add(BigInteger.ONE)) || end.compareTo(request.end()) < 0 || end.compareTo(boundary) < 0) {
                throw new GuardFailure("unsupported_receiver_or_boundary", "null");
            }
            NativeCopyObservationExport.Budget budget = new NativeCopyObservationExport.Budget(seconds, System::nanoTime);
            MessageDigest rowsHash = MessageDigest.getInstance("SHA-256");
            BigInteger previous = boundary;
            long count = 0;
            try (PreparedStatement statement = connection.prepareStatement(SQL + FROM + " ORDER BY SEQUENCE_NUMBER")) {
                bind(statement, request, boundary, end);
                statement.setQueryTimeout(budget.seconds());
                statement.setFetchSize(1000);
                statement.setMaxRows((int)maxRows + 1);
                try (ResultSet rows = statement.executeQuery()) {
                    metadata(rows);
                    while (rows.next()) {
                        budget.seconds();
                        if (count == maxRows) {
                            throw new GuardFailure("guard_row_budget", "null");
                        }
                        String library = rows.getString(1), receiver = rows.getString(2);
                        BigDecimal decimal = rows.getBigDecimal(3);
                        BigInteger sequence = decimal.toBigIntegerExact();
                        String code = rows.getString(4), type = rows.getString(5), object = rows.getString(6);
                        String objectType = rows.getString(7), jid = rows.getString(8);
                        String minimized = rows.getString(9), fieldMinimized = rows.getString(10);
                        String flag = rows.getString(11), nulls = rows.getString(12);
                        long lengthValue = rows.getLong(13);
                        Long length = rows.wasNull() ? null : lengthValue;
                        byte[] data = null;
                        boolean bodyComplete = false;
                        try (InputStream input = rows.getBinaryStream(14)) {
                            if (length == null) {
                                bodyComplete = input == null;
                            } else if (length >= 0 && length <= 65536 && input != null) {
                                data = input.readNBytes(length.intValue() + 1);
                                bodyComplete = data.length == length && input.read() == -1;
                            }
                        }
                        String row = "{\"receiver_library\":" + nullable(library) + ",\"receiver_name\":" + nullable(receiver) + ",\"sequence\":"
                            + q(sequence.toString()) + ",\"journal_code\":" + nullable(code) + ",\"journal_entry_type\":" + nullable(type)
                            + ",\"object\":" + nullable(object) + ",\"object_type\":" + nullable(objectType) + ",\"jid_hex\":" + nullable(jid)
                            + ",\"minimized_entry_data\":" + nullable(minimized) + ",\"minimized_on_field_boundary\":" + nullable(fieldMinimized)
                            + ",\"indicator_flag\":" + nullable(flag) + ",\"null_value_indicators\":" + nullable(nulls) + ",\"declared_payload_bytes\":"
                            +(length == null ? "null" : length) + ",\"entry_data_bytes\":" +(data == null ? "null" : data.length) + ",\"entry_data_base64\":"
                            +(data == null ? "null" : q(Base64.getEncoder().encodeToString(data))) + ",\"entry_data_sha256\":" +(data == null ? "null" : q(NativeCopyObservationExport.sha(data)))
                            + "}";
                        if (entries.length() + row.length() > MAX_METADATA) {
                            throw new GuardFailure("guard_byte_budget", "null");
                        }
                        if (count > 0) {
                            entries.append(',');
                        }
                        entries.append(row);
                        rowsHash.update((row + "\n").getBytes(StandardCharsets.UTF_8));
                        count++;
                        if (!request.receiverLibrary().equals(library) || !request.receiverName().equals(receiver) || sequence.compareTo(previous) <= 0
                            || sequence.compareTo(end) > 0) {
                            throw new GuardFailure("guard_position_mismatch", "null");
                        }
                        previous = sequence;
                        if ("F".equals(code) && Set.of("OP", "CL").contains(type) && bodyComplete) {
                            continue;
                        }
                        if (Set.of("D", "F").contains(code)) {
                            throw new GuardFailure("lifecycle_entry", "null");
                        }
                        if (!"R".equals(code) || !request.nativeObject().equals(object) || !"*FILE".equals(objectType) || jid == null
                            || !request.jidHex().equals(jid.toLowerCase(java.util.Locale.ROOT))) {
                            throw new GuardFailure("guard_identity_mismatch", "null");
                        }
                        if (!Set.of("PT", "PX", "UB", "UP", "DL").contains(type)) {
                            throw new GuardFailure("unsupported_record_entry_type", "null");
                        }
                        if (!bodyComplete || data == null || data.length != layout.recordLength() || !"0".equals(minimized) || !"0".equals(fieldMinimized)
                            || nulls == null || !(nulls.matches(" *") || nulls.equals("0".repeat(layout.fields().size()))) || Set.of("UB",
                            "DL").contains(type) && !"1".equals(flag)) {
                            throw new GuardFailure("guard_payload_incomplete", "null");
                        }
                    }
                    if (rows.getWarnings() != null || statement.getWarnings() != null || connection.getWarnings() != null) {
                        throw new GuardFailure("guard_warning", "null");
                    }
                }
            }
            count(connection, request, boundary, end, count, maxRows, budget);
            String json = "{\"prefix\":" + prefixJson + ",\"coverage\":{\"receiver_library\":" + q(request.receiverLibrary())
                + ",\"receiver_name\":" + q(request.receiverName()) + ",\"start_sequence\":" + q(boundary.add(BigInteger.ONE) .toString())
                + ",\"end_sequence\":" + q(end.toString()) + ",\"journal_codes\":\"D,F,R\",\"state\":\"complete\",\"row_count\":"
                + count + ",\"rows_sha256\":" + q(HexFormat.of().formatHex(rowsHash.digest())) + "},\"entries\":[" + entries + "]}";
            budget.seconds();
            if (json.getBytes(StandardCharsets.UTF_8).length > MAX_METADATA) {
                throw new GuardFailure("guard_byte_budget", "null");
            }
            return new Proof(request, layout.sha256(), boundary, prefix, json);
        } catch (Exception failure) {
            String code = failure instanceof GuardFailure guard ? guard.code() : "guard_unavailable";
            throw new GuardFailure(code, "{\"prefix\":" + prefixJson + ",\"coverage\":{\"state\":\"incomplete\"},\"entries\":["
                + entries + "]}");
        }
    }
    private static String nullable(String value) throws Exception {
        return value == null ? "null" : q(value);
    }
    private static void bind(PreparedStatement s, NativeBusinessImageExport.Request r, BigInteger boundary,
        BigInteger end) throws Exception {
        s.setString(1, r.journalLibrary());
        s.setString(2, r.journalName());
        s.setString(3, r.receiverLibrary());
        s.setString(4, r.receiverName());
        s.setBigDecimal(5, new BigDecimal(boundary.add(BigInteger.ONE)));
        s.setString(6, r.receiverLibrary());
        s.setString(7, r.receiverName());
        s.setBigDecimal(8, new BigDecimal(end));
        s.setString(9, r.fileName());
        s.setString(10, r.fileLibrary());
    }
    private static void metadata(ResultSet rows) throws Exception {
        var m = rows.getMetaData();
        String[] names = {
            "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT",
                "OBJECT_TYPE", "JID_HEX", "MINIMIZED_ENTRY_DATA", "MINIMIZED_ON_FIELD_BOUNDARY", "INDICATOR_FLAG",
                "NULL_VALUE_INDICATORS", "PAYLOAD_LENGTH", "ENTRY_DATA"
        };
        int[] types = {
            Types.VARCHAR, Types.VARCHAR, Types.DECIMAL, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.VARCHAR, Types.CHAR,
                Types.CHAR, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.BIGINT, Types.BLOB
        };
        if (m == null || m.getColumnCount() != names.length) {
            throw new IllegalArgumentException();
        }
        for (int n = 0; n < names.length; n++) {
            if (!names[n].equalsIgnoreCase(m.getColumnLabel(n + 1)) || m.getColumnType(n + 1) != types[n]) {
                throw new IllegalArgumentException();
            }
        }
        if (m.getPrecision(3) != 21 || m.getScale(3) != 0 || m.getPrecision(8) != 20) {
            throw new IllegalArgumentException();
        }
    }
    private static void count(Connection connection, NativeBusinessImageExport.Request request, BigInteger boundary,
        BigInteger end, long expected, long maxRows, NativeCopyObservationExport.Budget budget) throws Exception {
        String sql = "SELECT BIGINT(COUNT(*)) AS ENTRY_COUNT FROM (SELECT SEQUENCE_NUMBER" + FROM + " FETCH FIRST " +(maxRows
            + 1) + " ROWS ONLY) QD_COUNT";
        try (PreparedStatement s = connection.prepareStatement(sql)) {
            bind(s, request, boundary, end);
            s.setQueryTimeout(budget.seconds());
            try (ResultSet rows = s.executeQuery()) {
                var m = rows.getMetaData();
                if (m == null || m.getColumnCount() != 1 || !"ENTRY_COUNT".equals(m.getColumnLabel(1)) || m.getColumnType(1) != Types.BIGINT
                    || !rows.next() || !BigDecimal.valueOf(expected).equals(rows.getBigDecimal(1)) || rows.next()) {
                    throw new GuardFailure("guard_count_mismatch", "null");
                }
            }
        }
    }
}
