package io.quadringent.as400;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400Text;
import com.ibm.as400.access.ProgramCall;
import com.ibm.as400.access.ProgramParameter;
import com.ibm.as400.access.SecureAS400;
import com.ibm.as400.access.SocketProperties;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.Reader;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
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
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Properties;
import java.util.Set;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/** Positive DATA-member JID observations, not format, generation or historical admission.
 * Blocking I/O requires an outer supervised process deadline as well as these checks.
 */
public final class ReadOnlyMemberIdentity {
    private static final Pattern NAME = Pattern.compile("[A-Z0-9_$#@]{1,10}");
    private static final Pattern SHA = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern HOST = Pattern.compile("[A-Za-z0-9][A-Za-z0-9.-]{0,252}");
    private static final Pattern USER = Pattern.compile("[A-Za-z0-9_$#@]{1,128}");
    private static final BigInteger MAX_SEQUENCE = BigInteger.TEN.pow(21).subtract(BigInteger.ONE);
    private static final Set<String> ROW_TYPES = Set.of("PT", "PX", "UB", "UP", "DL");
    private static final int MEMBER_BYTES = 4096;
    private static final String[] LABELS = {"JID_HEX", "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER",
            "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT", "OBJECT_TYPE"};
    private static final int[] WIDTHS = {20, 10, 10, 21, 1, 2, 30, 10};

    private ReadOnlyMemberIdentity() { }

    public record Request(String journalLibrary, String journalName, String receiverLibrary, String receiverName,
                          String fileLibrary, String fileName, String memberName, BigInteger start, BigInteger end,
                          String scopeSha256) {
        public Request {
            for (String name : new String[]{journalLibrary, journalName, receiverLibrary, receiverName,
                    fileLibrary, fileName, memberName}) requireName(name);
            if (start == null || end == null || start.signum() < 1 || start.compareTo(MAX_SEQUENCE) > 0
                    || end.signum() < 0 || end.compareTo(MAX_SEQUENCE) > 0
                    || end.compareTo(start.subtract(BigInteger.ONE)) < 0
                    || scopeSha256 == null || !SHA.matcher(scopeSha256).matches()) {
                throw new IllegalArgumentException("invalid member identity request");
            }
        }
    }

    public record Limits(int maxMetadataBytes, int maxSeconds) {
        public Limits {
            if (maxMetadataBytes < 1 || maxMetadataBytes > 65536 || maxSeconds < 1 || maxSeconds > 60) {
                throw new IllegalArgumentException("invalid member identity budget");
            }
        }
        public static Limits defaults() { return new Limits(65536, 60); }
    }

    /** Parameter zero is output; the error parameter is input/output. No credentials occur here. */
    public record ProgramRequest(String program, List<byte[]> parameters, int receiverLength, int errorIndex) {
        public ProgramRequest {
            if (!Set.of("QUSRMBRD", "QJORJIDI").contains(program) || receiverLength < 1
                    || receiverLength > MEMBER_BYTES || parameters == null || parameters.size() > 8
                    || errorIndex < 1 || errorIndex >= parameters.size()) {
                throw new IllegalArgumentException("invalid native program request");
            }
            parameters = copy(parameters);
        }
        @Override public List<byte[]> parameters() { return copy(parameters); }
    }

    public record NativeReply(boolean succeeded, byte[] receiver, byte[] errorCode) {
        public NativeReply {
            if (receiver != null && receiver.length > MEMBER_BYTES || errorCode != null && errorCode.length > 16) {
                throw new IllegalArgumentException("native output exceeds budget");
            }
            receiver = receiver == null ? null : receiver.clone();
            errorCode = errorCode == null ? null : errorCode.clone();
        }
        @Override public byte[] receiver() { return receiver == null ? null : receiver.clone(); }
        @Override public byte[] errorCode() { return errorCode == null ? null : errorCode.clone(); }
    }

    @FunctionalInterface
    public interface NativeProgram {
        NativeReply call(ProgramRequest request, int timeoutSeconds) throws Exception;
    }

    public record NativeOriginal(String program, String format, byte[] receiver, byte[] errorCode) {
        public NativeOriginal { receiver = receiver.clone(); errorCode = errorCode.clone(); }
        @Override public byte[] receiver() { return receiver.clone(); }
        @Override public byte[] errorCode() { return errorCode.clone(); }
    }

    public record Witness(String jidHex, String receiverLibrary, String receiverName, String sequence,
                          String code, String entryType, String object, String objectType) { }

    public static final class Result {
        private final Request request;
        private final String jidHex;
        private final List<NativeOriginal> originals;
        private final List<Witness> witnesses;
        private final byte[] metadata;

        private Result(Request request, String jidHex, List<NativeOriginal> originals, List<Witness> witnesses, byte[] metadata) {
            this.request = request;
            this.jidHex = jidHex;
            this.originals = List.copyOf(originals);
            this.witnesses = List.copyOf(witnesses);
            this.metadata = metadata.clone();
        }
        public Request request() { return request; }
        public String jidHex() { return jidHex; }
        public List<NativeOriginal> originals() { return originals; }
        public List<Witness> witnesses() { return witnesses; }
        public byte[] metadata() { return metadata.clone(); }
    }

    public static final class IdentityFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private IdentityFailure(String code) { super("native_member_identity_error=" + code); this.code = code; }
        public String code() { return code; }
    }

    /** JDBC and native program transports are borrowed and remain owned by the caller. */
    public static Result collect(Connection connection, NativeProgram program, int ccsid, Request request, Limits limits)
            throws IdentityFailure {
        return collect(connection, program, ccsid, request, limits, System::nanoTime, Instant::now);
    }

    static Result collect(Connection connection, NativeProgram program, int ccsid, Request request, Limits limits,
                          LongSupplier monotonic, Supplier<Instant> now) throws IdentityFailure {
        try {
            return collect(connection, program, ccsid, request, new Budget(limits, monotonic), now);
        } catch (IdentityFailure failure) { throw failure; }
        catch (Exception failure) { throw new IdentityFailure("identity_unavailable"); }
    }

    private static Result collect(Connection connection, NativeProgram program, int ccsid, Request request,
                                  Budget budget, Supplier<Instant> now) throws Exception {
        if (connection == null || program == null || request == null || ccsid < 1 || ccsid > 65534) {
            throw new IdentityFailure("invalid_input");
        }
        budget.check();
        if (request.end().compareTo(request.start()) < 0) throw new IdentityFailure("unsupported_member_jid");
        Instant started = instant(now);
        budget.check(); connection.setReadOnly(true); budget.check();
        List<NativeOriginal> originals = new ArrayList<>();
        byte[] before = member(program, ccsid, request, originals, budget);
        Witness first = witness(connection, request, budget);
        resolve(program, ccsid, request, first, originals, budget);
        Witness last = witness(connection, request, budget);
        if (!first.jidHex().equals(last.jidHex())) throw new IdentityFailure("identity_changed");
        resolve(program, ccsid, request, last, originals, budget);
        byte[] after = member(program, ccsid, request, originals, budget);
        // Creation/restore observations are guards, not native generation identifiers.
        for (int offset : new int[]{8, 18, 28, 38, 58, 186}) {
            int size = offset == 58 || offset == 186 ? 13 : 10;
            if (!Arrays.equals(Arrays.copyOfRange(before, offset, offset + size),
                    Arrays.copyOfRange(after, offset, offset + size))) throw new IdentityFailure("identity_changed");
        }
        Instant completed = instant(now);
        if (completed.isBefore(started)) throw new IdentityFailure("clock_invalid");
        byte[] metadata = metadata(request, ccsid, originals, List.of(first, last), started, completed);
        if (metadata.length > budget.limits.maxMetadataBytes()) throw new IdentityFailure("metadata_budget_exceeded");
        budget.check();
        return new Result(request, first.jidHex(), originals, List.of(first, last), metadata);
    }

    private static byte[] member(NativeProgram program, int ccsid, Request request,
                                 List<NativeOriginal> originals, Budget budget) throws Exception {
        ProgramRequest call = new ProgramRequest("QUSRMBRD", List.of(new byte[0], integer(MEMBER_BYTES),
                encode("MBRD0200", 8, ccsid), encode(request.fileName() + " ".repeat(10 - request.fileName().length())
                        + request.fileLibrary(), 20, ccsid), encode(request.memberName(), 10, ccsid),
                encode("0", 1, ccsid), errorBuffer(), encode("0", 1, ccsid)), MEMBER_BYTES, 6);
        NativeReply reply = nativeCall(program, call, budget);
        byte[] raw = reply.receiver();
        int returned = fullReply(raw, MEMBER_BYTES, 266);
        requireField(raw, 8, 10, request.fileName(), ccsid);
        requireField(raw, 18, 10, request.fileLibrary(), ccsid);
        requireField(raw, 28, 10, request.memberName(), ccsid);
        requireField(raw, 38, 10, "PF", ccsid);
        for (int offset : new int[]{134, 135, 136}) requireField(raw, offset, 1, "0", ccsid);
        dateField(raw, 58, false, ccsid); dateField(raw, 186, true, ccsid);
        int extra = number(raw, 244), length = number(raw, 248);
        if (extra < 266 || length < 126 || extra > returned - length) throw new IdentityFailure("native_reply_invalid");
        requireField(raw, extra + 124, 1, "0", ccsid);
        requireField(raw, extra + 125, 1, "0", ccsid);
        byte[] returnedBytes = Arrays.copyOf(raw, returned);
        originals.add(new NativeOriginal(call.program(), "MBRD0200", returnedBytes, reply.errorCode()));
        return returnedBytes;
    }

    private static void resolve(NativeProgram program, int ccsid, Request request, Witness witness,
                                List<NativeOriginal> originals, Budget budget) throws Exception {
        byte[] jid = HexFormat.of().parseHex(witness.jidHex());
        ProgramRequest call = new ProgramRequest("QJORJIDI", List.of(new byte[0], integer(94),
                encode(request.journalName() + " ".repeat(10 - request.journalName().length())
                        + request.journalLibrary(), 20, ccsid), jid, encode("RJID0100", 8, ccsid), errorBuffer()), 94, 5);
        NativeReply reply = nativeCall(program, call, budget);
        byte[] raw = reply.receiver();
        if (fullReply(raw, 94, 94) != 94) throw new IdentityFailure("native_reply_invalid");
        requireField(raw, 8, 10, request.journalName(), ccsid);
        requireField(raw, 18, 10, request.journalLibrary(), ccsid);
        if (!Arrays.equals(jid, Arrays.copyOfRange(raw, 28, 38))) throw new IdentityFailure("native_reply_invalid");
        requireField(raw, 38, 10, request.fileName(), ccsid);
        requireField(raw, 48, 10, request.fileLibrary(), ccsid);
        requireField(raw, 58, 10, request.memberName(), ccsid);
        requireField(raw, 68, 10, "*QDDS", ccsid);
        requireField(raw, 78, 16, "", ccsid);
        originals.add(new NativeOriginal(call.program(), "RJID0100", raw, reply.errorCode()));
    }

    private static NativeReply nativeCall(NativeProgram program, ProgramRequest request, Budget budget) throws Exception {
        budget.check();
        NativeReply reply = program.call(request, budget.querySeconds());
        budget.check();
        if (reply == null || !reply.succeeded()) throw new IdentityFailure("native_program_failed");
        byte[] error = reply.errorCode();
        if (error == null || error.length != 16 || number(error, 0) != 16 || number(error, 4) != 0) {
            throw new IdentityFailure("native_program_failed");
        }
        for (int i = 8; i < 16; i++) if (error[i] != 0) throw new IdentityFailure("native_reply_invalid");
        return reply;
    }

    private static int fullReply(byte[] raw, int expected, int minimum) throws IdentityFailure {
        if (raw == null || raw.length != expected) throw new IdentityFailure("native_reply_invalid");
        int returned = number(raw, 0), available = number(raw, 4);
        if (returned < minimum || returned > raw.length || available != returned) throw new IdentityFailure("native_reply_invalid");
        return returned;
    }

    private static Witness witness(Connection connection, Request request, Budget budget) throws Exception {
        // Current-object selection is intentional ONLY for this positive witness, not history coverage.
        String sql = "SELECT HEX(JOURNAL_IDENTIFIER) AS JID_HEX, RECEIVER_LIBRARY, RECEIVER_NAME, SEQUENCE_NUMBER, "
                + "JOURNAL_CODE, JOURNAL_ENTRY_TYPE, OBJECT, OBJECT_TYPE FROM TABLE(QSYS2.DISPLAY_JOURNAL("
                + "JOURNAL_LIBRARY => ?, JOURNAL_NAME => ?, STARTING_RECEIVER_LIBRARY => ?, "
                + "STARTING_RECEIVER_NAME => ?, STARTING_SEQUENCE => ?, ENDING_RECEIVER_LIBRARY => ?, "
                + "ENDING_RECEIVER_NAME => ?, ENDING_SEQUENCE => ?, JOURNAL_CODES => 'F,R', EOF_DELAY => 0, "
                + "OBJECT_LIBRARY => ?, OBJECT_NAME => ?, OBJECT_OBJTYPE => '*FILE', OBJECT_MEMBER => ?)) "
                + "WHERE (JOURNAL_CODE = 'R' AND JOURNAL_ENTRY_TYPE IN ('PT','PX','UB','UP','DL')) "
                + "OR (JOURNAL_CODE = 'F' AND JOURNAL_ENTRY_TYPE = 'JM') ORDER BY SEQUENCE_NUMBER FETCH FIRST 1 ROW ONLY";
        Witness value;
        budget.check();
        try (PreparedStatement statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
            budget.check();
            String[] first = {request.journalLibrary(), request.journalName(), request.receiverLibrary(), request.receiverName()};
            for (int i = 0; i < first.length; i++) { budget.check(); statement.setString(i + 1, first[i]); budget.check(); }
            budget.check(); statement.setBigDecimal(5, new BigDecimal(request.start())); budget.check();
            budget.check(); statement.setString(6, request.receiverLibrary()); budget.check();
            budget.check(); statement.setString(7, request.receiverName()); budget.check();
            budget.check(); statement.setBigDecimal(8, new BigDecimal(request.end())); budget.check();
            budget.check(); statement.setString(9, request.fileLibrary()); budget.check();
            budget.check(); statement.setString(10, request.fileName()); budget.check();
            budget.check(); statement.setString(11, request.memberName()); budget.check();
            budget.check(); statement.setQueryTimeout(budget.querySeconds()); budget.check();
            budget.check(); statement.setFetchSize(1); budget.check();
            budget.check(); admitMetadata(statement.getMetaData(), budget); budget.check();
            try (ResultSet rows = statement.executeQuery()) {
                budget.check(); admitMetadata(rows.getMetaData(), budget); budget.check();
                if (!next(rows, budget)) throw new IdentityFailure("unsupported_member_jid");
                String jid = string(rows, 1, budget);
                if (jid == null || !jid.matches("[0-9A-Fa-f]{20}") || jid.equals("0".repeat(20))) {
                    throw new IdentityFailure("witness_invalid");
                }
                String library = token(string(rows, 2, budget)), receiver = token(string(rows, 3, budget));
                budget.check(); BigDecimal decimal = rows.getBigDecimal(4); budget.check();
                String sequence = sequence(decimal, request);
                String code = token(string(rows, 5, budget)), type = token(string(rows, 6, budget));
                String object = string(rows, 7, budget), objectType = token(string(rows, 8, budget));
                String expectedObject = padded(request.fileName()) + padded(request.fileLibrary()) + padded(request.memberName());
                if (!request.receiverLibrary().equals(library) || !request.receiverName().equals(receiver)
                        || !expectedObject.equals(object) || !Set.of("*FILE", "*QDDS").contains(objectType)
                        || !(code.equals("R") && ROW_TYPES.contains(type) || code.equals("F") && type.equals("JM"))) {
                    throw new IdentityFailure("witness_invalid");
                }
                value = new Witness(jid.toLowerCase(Locale.ROOT), library, receiver, sequence, code, type, object, objectType);
                if (next(rows, budget)) throw new IdentityFailure("witness_invalid");
            }
            budget.check();
        }
        budget.check();
        return value;
    }

    private static void admitMetadata(ResultSetMetaData metadata, Budget budget) throws Exception {
        if (metadata == null) throw new IdentityFailure("metadata_invalid");
        budget.check(); int count = metadata.getColumnCount(); budget.check();
        if (count != LABELS.length) throw new IdentityFailure("metadata_invalid");
        for (int i = 1; i <= count; i++) {
            budget.check(); String label = metadata.getColumnLabel(i); budget.check();
            budget.check(); int type = metadata.getColumnType(i); budget.check();
            budget.check(); int precision = metadata.getPrecision(i); budget.check();
            if (!LABELS[i - 1].equals(label) || precision != WIDTHS[i - 1]) throw new IdentityFailure("metadata_invalid");
            if (i == 4) {
                budget.check(); int scale = metadata.getScale(i); budget.check();
                if ((type != Types.NUMERIC && type != Types.DECIMAL) || scale != 0) throw new IdentityFailure("metadata_invalid");
            } else if (type != Types.CHAR && type != Types.VARCHAR) throw new IdentityFailure("metadata_invalid");
        }
    }

    private static String sequence(BigDecimal value, Request request) throws IdentityFailure {
        if (value == null || value.signum() < 1 || value.precision() > 21
                || (long) value.precision() - value.scale() > 21 || value.scale() > 21) throw new IdentityFailure("witness_invalid");
        BigInteger exact;
        try { exact = value.toBigIntegerExact(); } catch (ArithmeticException failure) { throw new IdentityFailure("witness_invalid"); }
        if (exact.compareTo(request.start()) < 0 || exact.compareTo(request.end()) > 0) throw new IdentityFailure("witness_invalid");
        return exact.toString();
    }

    private static boolean next(ResultSet rows, Budget budget) throws Exception {
        budget.check(); boolean value = rows.next(); budget.check(); return value;
    }
    private static String string(ResultSet rows, int column, Budget budget) throws Exception {
        budget.check(); String value = rows.getString(column); budget.check(); return value;
    }
    private static String token(String value) throws IdentityFailure {
        if (value == null || !value.matches("[A-Z0-9_$#@*]{1,10} *")) throw new IdentityFailure("witness_invalid");
        return value.stripTrailing();
    }
    private static String padded(String value) { return value + " ".repeat(10 - value.length()); }
    private static void requireName(String name) {
        if (name == null || !NAME.matcher(name).matches()) throw new IllegalArgumentException("invalid native name");
    }
    private static int number(byte[] value, int offset) { return ByteBuffer.wrap(value, offset, 4).getInt(); }
    private static byte[] integer(int value) { return ByteBuffer.allocate(4).putInt(value).array(); }
    private static byte[] errorBuffer() { return ByteBuffer.allocate(16).putInt(16).array(); }
    private static byte[] encode(String value, int width, int ccsid) { return new AS400Text(width, ccsid).toBytes(value); }
    private static void requireField(byte[] raw, int offset, int length, String value, int ccsid) throws IdentityFailure {
        if (!Arrays.equals(Arrays.copyOfRange(raw, offset, offset + length), encode(value, length, ccsid))) {
            throw new IdentityFailure("native_reply_invalid");
        }
    }
    private static void dateField(byte[] raw, int offset, boolean blankAllowed, int ccsid) throws IdentityFailure {
        String value = (String) new AS400Text(13, ccsid).toObject(raw, offset);
        if (blankAllowed && value.equals(" ".repeat(13))) return;
        if (!value.matches("[0-9]{13}")) throw new IdentityFailure("native_reply_invalid");
        try {
            int year = 1900 + Integer.parseInt(value.substring(0, 3));
            java.time.LocalDateTime.of(year, Integer.parseInt(value.substring(3, 5)), Integer.parseInt(value.substring(5, 7)),
                    Integer.parseInt(value.substring(7, 9)), Integer.parseInt(value.substring(9, 11)), Integer.parseInt(value.substring(11, 13)));
        } catch (RuntimeException failure) { throw new IdentityFailure("native_reply_invalid"); }
    }
    private static List<byte[]> copy(List<byte[]> values) {
        List<byte[]> result = new ArrayList<>(values.size());
        for (byte[] value : values) result.add(value.clone());
        return List.copyOf(result);
    }
    private static Instant instant(Supplier<Instant> now) throws IdentityFailure {
        Instant value = now.get();
        if (value == null || value.atOffset(ZoneOffset.UTC).getYear() < 1 || value.atOffset(ZoneOffset.UTC).getYear() > 9999) {
            throw new IdentityFailure("clock_invalid");
        }
        return value;
    }

    private static byte[] metadata(Request request, int ccsid, List<NativeOriginal> originals,
                                   List<Witness> witnesses, Instant started, Instant completed) throws Exception {
        StringBuilder json = new StringBuilder(20000);
        json.append("{\"format_version\":\"quadringent-member-data-identity-v1\",\"admission\":\"not_evaluated\",\"scope_sha256\":\"")
                .append(request.scopeSha256()).append("\",\"jid_hex\":\"").append(witnesses.getFirst().jidHex())
                .append("\",\"ccsid\":").append(ccsid).append(",\"started_at\":\"").append(started)
                .append("\",\"completed_at\":\"").append(completed).append("\",\"request\":{");
        String[] names = {"journal_library", "journal_name", "receiver_library", "receiver_name", "file_library",
                "file_name", "member_name", "start_sequence", "end_sequence"};
        String[] fields = {request.journalLibrary(), request.journalName(), request.receiverLibrary(), request.receiverName(),
                request.fileLibrary(), request.fileName(), request.memberName(), request.start().toString(), request.end().toString()};
        for (int i = 0; i < fields.length; i++) {
            if (i > 0) json.append(',');
            json.append('"').append(names[i]).append("\":\"").append(fields[i]).append('"');
        }
        json.append("},\"witnesses\":[");
        for (int i = 0; i < witnesses.size(); i++) {
            Witness witness = witnesses.get(i); if (i > 0) json.append(',');
            json.append("{\"jid_hex\":\"").append(witness.jidHex()).append("\",\"receiver_library\":\"").append(witness.receiverLibrary())
                    .append("\",\"receiver_name\":\"").append(witness.receiverName()).append("\",\"sequence\":\"").append(witness.sequence())
                    .append("\",\"code\":\"").append(witness.code()).append("\",\"entry_type\":\"").append(witness.entryType())
                    .append("\",\"object\":\"").append(witness.object()).append("\",\"object_type\":\"").append(witness.objectType()).append("\"}");
        }
        json.append("],\"native_originals\":[");
        for (int i = 0; i < originals.size(); i++) {
            NativeOriginal original = originals.get(i); if (i > 0) json.append(',');
            json.append("{\"program\":\"").append(original.program()).append("\",\"format\":\"").append(original.format())
                    .append("\",\"receiver_hex\":\"").append(HexFormat.of().formatHex(original.receiver()))
                    .append("\",\"receiver_sha256\":\"").append(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(original.receiver())))
                    .append("\",\"error_code_hex\":\"").append(HexFormat.of().formatHex(original.errorCode())).append("\"}");
        }
        return json.append("]}\n").toString().getBytes(StandardCharsets.US_ASCII);
    }

    private static final class Budget {
        final Limits limits;
        final LongSupplier clock;
        final long started;
        long previous;
        Budget(Limits limits, LongSupplier clock) {
            this.limits = limits; this.clock = clock; this.started = clock.getAsLong(); this.previous = started;
        }
        void check() throws IdentityFailure {
            long current = clock.getAsLong();
            if (current < previous || current - started < 0 || current - started >= limits.maxSeconds() * 1_000_000_000L) {
                throw new IdentityFailure("deadline_exceeded");
            }
            previous = current;
        }
        int querySeconds() throws IdentityFailure {
            check();
            long remaining = limits.maxSeconds() * 1_000_000_000L - (previous - started);
            int seconds = (int) (remaining / 1_000_000_000L);
            if (seconds < 1) throw new IdentityFailure("deadline_exceeded");
            return seconds;
        }
    }

    interface Session extends AutoCloseable {
        Connection connection();
        NativeProgram program();
        int ccsid();
        @Override void close() throws SQLException;
    }
    @FunctionalInterface interface SessionFactory {
        Session open(String host, String user, String password, int timeoutSeconds) throws Exception;
    }

    public static void main(String[] args) {
        int status = run(args, System.in, System.out, System.err, ReadOnlyMemberIdentity::connect, System::nanoTime, Instant::now);
        if (status != 0) System.exit(status);
    }

    static int run(String[] args, InputStream input, OutputStream output, OutputStream errors, SessionFactory factory,
                   LongSupplier monotonic, Supplier<Instant> now) {
        try {
            if (args == null || args.length != 12) throw new IllegalArgumentException("invalid arguments");
            Request request = new Request(args[0], args[1], args[2], args[3], args[4], args[5], args[6],
                    cliSequence(args[7]), cliSequence(args[8]), args[9]);
            Limits limits = new Limits(Integer.parseInt(args[10]), Integer.parseInt(args[11]));
            Budget budget = new Budget(limits, monotonic);
            Reader reader = new InputStreamReader(input, StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT));
            String host = credential(reader, 253, budget), user = credential(reader, 128, budget), secret = credential(reader, 4096, budget);
            if (!HOST.matcher(host).matches() || !USER.matcher(user).matches() || secret.isBlank()) {
                throw new IllegalArgumentException("invalid connection input");
            }
            requireTls();
            Result result;
            budget.check();
            try (Session session = factory.open(host, user, secret, budget.querySeconds())) {
                budget.check();
                budget.check(); Connection connection = session.connection(); budget.check();
                budget.check(); NativeProgram program = session.program(); budget.check();
                budget.check(); int ccsid = session.ccsid(); budget.check();
                result = collect(connection, program, ccsid, request, budget, now);
            }
            budget.check();
            output.write(result.metadata()); budget.check();
            output.flush(); budget.check();
            if (output instanceof PrintStream printed && printed.checkError()) throw new IdentityFailure("output_failed");
            budget.check();
            return 0;
        } catch (IdentityFailure failure) { return failure(errors, failure.code()); }
        catch (IllegalArgumentException failure) { return failure(errors, "invalid_input"); }
        catch (Exception failure) { return failure(errors, "identity_unavailable"); }
    }

    private static int failure(OutputStream errors, String code) {
        try { errors.write(("native_member_identity_error=" + code + "\n").getBytes(StandardCharsets.US_ASCII)); errors.flush(); }
        catch (IOException ignored) { }
        return 2;
    }
    private static BigInteger cliSequence(String value) {
        if (value == null || value.length() > 21 || !value.matches("0|[1-9][0-9]{0,20}")) {
            throw new IllegalArgumentException("invalid native sequence");
        }
        return new BigInteger(value);
    }
    private static String credential(Reader reader, int maximum, Budget budget) throws Exception {
        StringBuilder result = new StringBuilder();
        while (true) {
            budget.check(); int value = reader.read(); budget.check();
            if (value < 0 || value == '\r' || value == 0) throw new IllegalArgumentException("invalid stdin");
            if (value == '\n') return result.toString();
            if (result.length() == maximum) throw new IllegalArgumentException("stdin budget exceeded");
            result.append((char) value);
        }
    }
    private static void requireTls() throws IdentityFailure {
        for (String key : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String value = System.getProperty(key);
            if (value != null && Set.of("true", "1", "yes", "on").contains(value.strip().toLowerCase(Locale.ROOT))) {
                throw new IdentityFailure("tls_required");
            }
        }
    }

    private static Session connect(String host, String user, String password, int timeout) throws Exception {
        requireTls(); TlsTrust.install(true);
        AS400 system = new SecureAS400(host, user, password.toCharArray());
        Connection connection = null;
        try {
            system.setGuiAvailable(false); system.setMustUseSockets(true);
            SocketProperties sockets = new SocketProperties(); sockets.setSoTimeout(timeout * 1000); system.setSocketProperties(sockets);
            Class.forName("com.ibm.as400.access.AS400JDBCDriver");
            Properties properties = NativeVerificationSourceExport.connectionProperties(user, password);
            properties.setProperty("socket timeout", Integer.toString(timeout * 1000));
            properties.setProperty("login timeout", Integer.toString(Math.min(15, timeout)));
            connection = DriverManager.getConnection("jdbc:as400://" + host, properties);
            Connection owned = connection;
            return new Session() {
                @Override public Connection connection() { return owned; }
                @Override public int ccsid() { return system.getCcsid(); }
                @Override public NativeProgram program() { return (request, seconds) -> call(system, request, seconds); }
                @Override public void close() throws SQLException {
                    try { owned.close(); } finally { system.disconnectAllServices(); }
                }
            };
        } catch (Exception failure) {
            try { if (connection != null) connection.close(); } finally { system.disconnectAllServices(); }
            throw failure;
        }
    }

    private static NativeReply call(AS400 system, ProgramRequest request, int timeout) throws Exception {
        List<byte[]> inputs = request.parameters();
        ProgramParameter[] parameters = new ProgramParameter[inputs.size()];
        for (int i = 0; i < inputs.size(); i++) {
            if (i == 0) parameters[i] = new ProgramParameter(request.receiverLength());
            else if (i == request.errorIndex()) parameters[i] = new ProgramParameter(inputs.get(i), 16);
            else parameters[i] = new ProgramParameter(inputs.get(i));
        }
        ProgramCall call = new ProgramCall(system, "/QSYS.LIB/" + request.program() + ".PGM", parameters);
        call.setTimeOut(timeout);
        boolean succeeded = call.run();
        return new NativeReply(succeeded, parameters[0].getOutputData(), parameters[request.errorIndex()].getOutputData());
    }
}
