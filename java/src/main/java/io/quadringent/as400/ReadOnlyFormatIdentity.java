package io.quadringent.as400;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.ProgramCall;
import com.ibm.as400.access.ProgramParameter;
import com.ibm.as400.access.SecureAS400;
import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.Charset;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/** One FILD0200 observation; no historical schema, generation or native admission.
 * Blocking native I/O also requires an outer supervised process deadline.
 */
public final class ReadOnlyFormatIdentity {
    private static final Pattern NAME = Pattern.compile("[A-Z0-9_$#@]{1,10}");
    private static final Pattern SHA = Pattern.compile("[0-9a-f]{64}");
    private static final int HEADER_BYTES = 256;
    private static final int MAX_RESPONSE_BYTES = 65536;

    private ReadOnlyFormatIdentity() { }

    public record Request(String fileLibrary, String fileName, String formatName, String scopeSha256) {
        public Request {
            for (String name : new String[]{fileLibrary, fileName, formatName}) {
                if (name == null || !NAME.matcher(name).matches()) throw new IllegalArgumentException("invalid native target");
            }
            if (scopeSha256 == null || !SHA.matcher(scopeSha256).matches()) throw new IllegalArgumentException("invalid scope digest");
        }
    }

    public record Limits(int maxResponseBytes, int maxMetadataBytes, int maxSeconds) {
        public Limits {
            if (maxResponseBytes < HEADER_BYTES || maxResponseBytes > MAX_RESPONSE_BYTES
                    || maxMetadataBytes < 1 || maxMetadataBytes > 262144 || maxSeconds < 1 || maxSeconds > 60) {
                throw new IllegalArgumentException("invalid format identity budget");
            }
        }
        public static Limits defaults() { return new Limits(16384, 65536, 60); }
    }

    /** Only collect can construct the fixed ten-parameter QDBRTVFD request. */
    public static final class ProgramRequest {
        private final int receiverLength;
        private final List<byte[]> parameters;

        private ProgramRequest(Request request, Charset encoding, int receiverLength) throws IdentityFailure {
            this.receiverLength = receiverLength;
            this.parameters = List.of(new byte[0], integer(receiverLength), new byte[0],
                    encode("FILD0200", 8, encoding), encode(padded(request.fileName()) + padded(request.fileLibrary()), 20, encoding),
                    encode(request.formatName(), 10, encoding), encode("0", 1, encoding),
                    encode("*LCL", 10, encoding), encode("*EXT", 10, encoding), errorBuffer());
        }
        public String program() { return "QDBRTVFD"; }
        public int receiverLength() { return receiverLength; }
        public int errorIndex() { return 9; }
        public List<byte[]> parameters() {
            List<byte[]> result = new ArrayList<>(parameters.size());
            for (byte[] parameter : parameters) result.add(parameter.clone());
            return List.copyOf(result);
        }
    }

    public record NativeReply(boolean succeeded, byte[] receiver, byte[] qualifiedTarget, byte[] errorCode) {
        public NativeReply {
            if (receiver != null && receiver.length > MAX_RESPONSE_BYTES
                    || qualifiedTarget != null && qualifiedTarget.length > 20 || errorCode != null && errorCode.length > 16) {
                throw new IllegalArgumentException("native response exceeds budget");
            }
            receiver = cloneOrNull(receiver); qualifiedTarget = cloneOrNull(qualifiedTarget); errorCode = cloneOrNull(errorCode);
        }
        @Override public byte[] receiver() { return cloneOrNull(receiver); }
        @Override public byte[] qualifiedTarget() { return cloneOrNull(qualifiedTarget); }
        @Override public byte[] errorCode() { return cloneOrNull(errorCode); }
    }

    @FunctionalInterface public interface NativeProgram {
        NativeReply call(ProgramRequest request, int timeoutSeconds) throws Exception;
    }

    public record Header(int bytesReturned, int bytesAvailable, int recordLength, String formatName,
                         String levelId, int fieldCount) { }

    public static final class NativeOriginal {
        private final byte[] receiver, qualifiedTarget, errorCode;
        private final String receiverSha256;
        private NativeOriginal(byte[] receiver, byte[] qualifiedTarget, byte[] errorCode) throws Exception {
            this.receiver = receiver.clone(); this.qualifiedTarget = qualifiedTarget.clone(); this.errorCode = errorCode.clone();
            this.receiverSha256 = hash(receiver);
        }
        public byte[] receiver() { return receiver.clone(); }
        public byte[] qualifiedTarget() { return qualifiedTarget.clone(); }
        public byte[] errorCode() { return errorCode.clone(); }
        public String receiverSha256() { return receiverSha256; }
    }

    public static final class Result {
        private final Request request;
        private final int ccsid;
        private final Header header;
        private final NativeOriginal original;
        private final byte[] rawLayout, metadata;
        private final String rawLayoutSha256;
        private Result(Request request, int ccsid, Header header, NativeOriginal original,
                       byte[] rawLayout, String rawLayoutSha256, byte[] metadata) {
            this.request = request; this.ccsid = ccsid; this.header = header; this.original = original;
            this.rawLayout = rawLayout.clone(); this.rawLayoutSha256 = rawLayoutSha256; this.metadata = metadata.clone();
        }
        public Request request() { return request; }
        public int ccsid() { return ccsid; }
        public Header header() { return header; }
        public NativeOriginal original() { return original; }
        public byte[] rawLayout() { return rawLayout.clone(); }
        public String rawLayoutSha256() { return rawLayoutSha256; }
        public byte[] metadata() { return metadata.clone(); }
    }

    public static final class IdentityFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private IdentityFailure(String code) { super("native_format_identity_error=" + code); this.code = code; }
        public String code() { return code; }
    }

    /** The caller retains the transport and must establish the effective native-job CCSID separately. */
    public static Result collect(NativeProgram program, int ccsid, Request request, Limits limits) throws IdentityFailure {
        return collect(program, ccsid, request, limits, System::nanoTime, Instant::now);
    }

    static Result collect(NativeProgram program, int ccsid, Request request, Limits limits,
                          LongSupplier monotonic, Supplier<Instant> now) throws IdentityFailure {
        try {
            if (program == null || request == null || limits == null || monotonic == null || now == null) {
                throw new IdentityFailure("invalid_input");
            }
            Budget budget = new Budget(limits, monotonic);
            Charset encoding = encoding(ccsid);
            budget.check(); Instant started = instant(now); budget.check();
            ProgramRequest call = new ProgramRequest(request, encoding, limits.maxResponseBytes());
            budget.check(); NativeReply reply = program.call(call, budget.querySeconds()); budget.check();
            if (reply == null || !reply.succeeded()) throw new IdentityFailure("native_program_failed");
            byte[] error = reply.errorCode();
            if (error == null || error.length != 16 || number(error, 0) != 16 || number(error, 4) != 0) {
                throw new IdentityFailure("native_program_failed");
            }
            for (int i = 8; i < 16; i++) if (error[i] != 0) throw new IdentityFailure("native_reply_invalid");
            byte[] target = reply.qualifiedTarget();
            if (target == null || !Arrays.equals(target,
                    encode(padded(request.fileName()) + padded(request.fileLibrary()), 20, encoding))) {
                throw new IdentityFailure("native_target_mismatch");
            }
            byte[] raw = reply.receiver();
            if (raw == null || raw.length != limits.maxResponseBytes()) throw new IdentityFailure("native_reply_invalid");
            int returned = number(raw, 0), available = number(raw, 4), recordLength = number(raw, 66);
            if (returned < HEADER_BYTES || returned > raw.length || available != returned || recordLength < 1) {
                throw new IdentityFailure("native_reply_invalid");
            }
            if (!Arrays.equals(Arrays.copyOfRange(raw, 70, 80), encode(request.formatName(), 10, encoding))) {
                throw new IdentityFailure("native_format_mismatch");
            }
            String level = decode(Arrays.copyOfRange(raw, 80, 93), encoding);
            if (!level.matches("[!-~]{13}")) throw new IdentityFailure("native_level_invalid");
            int fields = Short.toUnsignedInt(ByteBuffer.wrap(raw, 143, 2).getShort());
            if (fields < 1 || fields > (returned - HEADER_BYTES) / 4) throw new IdentityFailure("native_reply_invalid");
            int offset = HEADER_BYTES;
            // Only field-section boundaries are checked; their contents remain opaque.
            for (int i = 0; i < fields; i++) {
                if (offset > returned - 4) throw new IdentityFailure("native_reply_invalid");
                int length = number(raw, offset);
                if (length < 4 || length > returned - offset) throw new IdentityFailure("native_reply_invalid");
                offset += length;
            }
            NativeOriginal original = new NativeOriginal(Arrays.copyOf(raw, returned), target, error);
            byte[] layout = Arrays.copyOfRange(raw, HEADER_BYTES, returned);
            String layoutSha256 = hash(layout);
            Header header = new Header(returned, available, recordLength, request.formatName(), level, fields);
            budget.check(); Instant completed = instant(now); budget.check();
            if (completed.isBefore(started)) throw new IdentityFailure("clock_invalid");
            byte[] metadata = metadata(request, ccsid, header, original, layoutSha256, started, completed, limits);
            budget.check();
            return new Result(request, ccsid, header, original, layout, layoutSha256, metadata);
        } catch (IdentityFailure failure) { throw failure; }
        catch (Exception failure) { throw new IdentityFailure("identity_unavailable"); }
    }

    /** Borrowed TLS transport, fixed read-only program and fixed output/error parameter positions. */
    public static NativeProgram program(AS400 system) {
        if (!(system instanceof SecureAS400)) throw new IllegalArgumentException("TLS native transport required");
        return (request, timeoutSeconds) -> {
            if (request == null || timeoutSeconds < 1 || timeoutSeconds > 60) throw new IdentityFailure("invalid_input");
            requireTls();
            List<byte[]> inputs = request.parameters();
            ProgramParameter[] parameters = new ProgramParameter[10];
            for (int i = 0; i < parameters.length; i++) {
                if (i == 0) parameters[i] = new ProgramParameter(request.receiverLength());
                else if (i == 2) parameters[i] = new ProgramParameter(20);
                else if (i == 9) parameters[i] = new ProgramParameter(inputs.get(i), 16);
                else parameters[i] = new ProgramParameter(inputs.get(i));
            }
            ProgramCall call = new ProgramCall(system, "/QSYS.LIB/QDBRTVFD.PGM", parameters);
            call.setTimeOut(timeoutSeconds);
            boolean succeeded = call.run();
            return new NativeReply(succeeded, parameters[0].getOutputData(), parameters[2].getOutputData(), parameters[9].getOutputData());
        };
    }

    private static void requireTls() throws IdentityFailure {
        for (String key : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String value = System.getProperty(key);
            if (value != null && Set.of("true", "1", "yes", "on").contains(value.strip().toLowerCase(Locale.ROOT))) {
                throw new IdentityFailure("tls_required");
            }
        }
    }

    private static Charset encoding(int ccsid) throws IdentityFailure {
        try {
            if (ccsid < 1 || ccsid > 65534) throw new IllegalArgumentException();
            Charset encoding = Charset.forName(ccsid == 37 ? "IBM037" : "Cp" + ccsid);
            if (encoding.newEncoder().maxBytesPerChar() != 1 || encoding.newDecoder().maxCharsPerByte() != 1) {
                throw new IllegalArgumentException();
            }
            return encoding;
        } catch (IllegalArgumentException failure) { throw new IdentityFailure("unsupported_ccsid"); }
    }
    private static byte[] encode(String text, int width, Charset encoding) throws IdentityFailure {
        try {
            if (text.length() > width) throw new IdentityFailure("native_reply_invalid");
            ByteBuffer bytes = encoding.newEncoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).encode(CharBuffer.wrap(text + " ".repeat(width - text.length())));
            byte[] result = new byte[bytes.remaining()]; bytes.get(result);
            if (result.length != width) throw new IdentityFailure("native_reply_invalid");
            return result;
        } catch (CharacterCodingException failure) { throw new IdentityFailure("unsupported_ccsid"); }
    }
    private static String decode(byte[] raw, Charset encoding) throws IdentityFailure {
        try {
            String text = encoding.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(raw)).toString();
            if (!Arrays.equals(raw, encode(text, raw.length, encoding))) throw new IdentityFailure("native_reply_invalid");
            return text;
        } catch (CharacterCodingException failure) { throw new IdentityFailure("unsupported_ccsid"); }
    }
    private static String padded(String value) { return value + " ".repeat(10 - value.length()); }
    private static int number(byte[] raw, int offset) { return ByteBuffer.wrap(raw, offset, 4).getInt(); }
    private static byte[] integer(int value) { return ByteBuffer.allocate(4).putInt(value).array(); }
    private static byte[] errorBuffer() { return ByteBuffer.allocate(16).putInt(16).array(); }
    private static byte[] cloneOrNull(byte[] value) { return value == null ? null : value.clone(); }
    private static String hash(byte[] value) throws Exception { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value)); }
    private static Instant instant(Supplier<Instant> now) throws IdentityFailure {
        Instant result = now.get();
        if (result == null || result.atOffset(ZoneOffset.UTC).getYear() < 1 || result.atOffset(ZoneOffset.UTC).getYear() > 9999) {
            throw new IdentityFailure("clock_invalid");
        }
        return result;
    }
    private static String quoted(String text) { return '"' + text.replace("\\", "\\\\").replace("\"", "\\\"") + '"'; }

    private static byte[] metadata(Request request, int ccsid, Header header, NativeOriginal original,
                                   String layoutSha256, Instant started, Instant completed, Limits limits) throws IdentityFailure {
        if (original.receiver.length * 2L + 2048 > limits.maxMetadataBytes()) throw new IdentityFailure("metadata_budget_exceeded");
        String json = "{\"format_version\":\"quadringent-format-identity-v1\",\"admission\":\"not_evaluated\","
                + "\"ccsid_provenance\":\"caller_declared\",\"layout_semantics\":\"opaque_native_bytes\","
                + "\"scope_sha256\":" + quoted(request.scopeSha256()) + ",\"file_library\":" + quoted(request.fileLibrary())
                + ",\"file_name\":" + quoted(request.fileName()) + ",\"format_name\":" + quoted(header.formatName())
                + ",\"level_id\":" + quoted(header.levelId()) + ",\"level_id_hex\":\""
                + HexFormat.of().formatHex(Arrays.copyOfRange(original.receiver, 80, 93)) + "\",\"ccsid\":" + ccsid
                + ",\"started_at\":" + quoted(started.toString()) + ",\"completed_at\":" + quoted(completed.toString())
                + ",\"bytes_returned\":" + header.bytesReturned() + ",\"bytes_available\":" + header.bytesAvailable()
                + ",\"record_length\":" + header.recordLength() + ",\"field_count\":" + header.fieldCount()
                + ",\"raw_layout_sha256\":" + quoted(layoutSha256) + ",\"native_original\":{\"program\":\"QDBRTVFD\","
                + "\"format\":\"FILD0200\",\"receiver_hex\":\"" + HexFormat.of().formatHex(original.receiver)
                + "\",\"receiver_sha256\":" + quoted(original.receiverSha256()) + ",\"qualified_target_hex\":\""
                + HexFormat.of().formatHex(original.qualifiedTarget) + "\",\"error_code_hex\":\""
                + HexFormat.of().formatHex(original.errorCode) + "\"}}\n";
        byte[] bytes = json.getBytes(StandardCharsets.US_ASCII);
        if (bytes.length > limits.maxMetadataBytes()) throw new IdentityFailure("metadata_budget_exceeded");
        return bytes;
    }

    private static final class Budget {
        private final Limits limits;
        private final LongSupplier clock;
        private final long started;
        private long previous;
        private Budget(Limits limits, LongSupplier clock) {
            this.limits = limits; this.clock = clock; this.started = clock.getAsLong(); this.previous = started;
        }
        private void check() throws IdentityFailure {
            long current = clock.getAsLong();
            if (current < previous || current - started < 0 || current - started >= limits.maxSeconds() * 1_000_000_000L) {
                throw new IdentityFailure("deadline_exceeded");
            }
            previous = current;
        }
        private int querySeconds() throws IdentityFailure {
            check();
            int seconds = (int) ((limits.maxSeconds() * 1_000_000_000L - (previous - started)) / 1_000_000_000L);
            if (seconds < 1) throw new IdentityFailure("deadline_exceeded");
            return seconds;
        }
    }
}
