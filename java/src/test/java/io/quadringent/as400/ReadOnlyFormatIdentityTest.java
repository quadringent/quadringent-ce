package io.quadringent.as400;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400Text;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;

/** Offline native buffers; no IBM i connection is opened. */
public final class ReadOnlyFormatIdentityTest {
    private static final String SHA = "a".repeat(64);
    private static final String LEVEL = "ABCDEF0123456";
    private static final int CCSID = 37;
    private static final int RETURNED = 480;
    private static final Instant NOW = Instant.parse("2026-10-16T08:00:00Z");
    private static int assertions;

    private ReadOnlyFormatIdentityTest() { }

    public static void main(String[] args) throws Exception {
        completeIdentityAndParameters();
        inputBounds();
        malformedReplies();
        deadlinesAndFailures();
        defensiveBuffers();
        System.out.println("ReadOnlyFormatIdentityTest: " + assertions + " assertions passed");
    }

    private static ReadOnlyFormatIdentity.Request request() {
        return new ReadOnlyFormatIdentity.Request("TESTLIB", "ITEMS", "ITEMSFMT", SHA);
    }

    private static ReadOnlyFormatIdentity.Result collect(Fake fake) throws Exception {
        return ReadOnlyFormatIdentity.collect(fake::call, CCSID, request(),
                ReadOnlyFormatIdentity.Limits.defaults(), () -> fake.nanos, () -> NOW);
    }

    private static void completeIdentityAndParameters() throws Exception {
        Fake fake = new Fake();
        var result = collect(fake);
        equal(1, fake.calls);
        equal(request(), result.request());
        equal(CCSID, result.ccsid());
        equal(LEVEL, result.header().levelId());
        equal("ITEMSFMT", result.header().formatName());
        equal(37, result.header().recordLength());
        equal(2, result.header().fieldCount());
        equal(RETURNED, result.header().bytesReturned());
        equal(RETURNED, result.header().bytesAvailable());
        equal(RETURNED, result.original().receiver().length);
        equal(224, result.rawLayout().length);
        equal(hash(result.original().receiver()), result.original().receiverSha256());
        equal(hash(result.rawLayout()), result.rawLayoutSha256());
        equal("QDBRTVFD", fake.request.program());
        equal(16384, fake.request.receiverLength());
        equal(9, fake.request.errorIndex());
        List<byte[]> parameters = fake.request.parameters();
        equal(10, parameters.size());
        equal(0, parameters.get(0).length);
        equal(16384, ByteBuffer.wrap(parameters.get(1)).getInt());
        equal(0, parameters.get(2).length);
        equal("FILD0200", text(parameters.get(3)));
        equal("ITEMS     TESTLIB   ", text(parameters.get(4)));
        equal("ITEMSFMT  ", text(parameters.get(5)));
        equal("0", text(parameters.get(6)));
        equal("*LCL      ", text(parameters.get(7)));
        equal("*EXT      ", text(parameters.get(8)));
        equal(16, parameters.get(9).length);
        equal(16, ByteBuffer.wrap(parameters.get(9)).getInt());
        check(fake.timeout > 0 && fake.timeout <= 60, "native timeout bounded");
        String json = new String(result.metadata(), StandardCharsets.US_ASCII);
        check(json.contains("\"format_version\":\"quadringent-format-identity-v1\""), "metadata version");
        check(json.contains("\"admission\":\"not_evaluated\""), "no native admission");
        check(json.contains("\"ccsid_provenance\":\"caller_declared\""), "CCSID provenance honest");
        check(json.contains("\"layout_semantics\":\"opaque_native_bytes\""), "no schema decoder claim");
        check(json.contains(LEVEL) && json.contains(result.original().receiverSha256()), "identity and original digest");
        check(!json.contains("cafebabefeedbeef"), "unreturned allocation tail omitted");
        check(result.metadata().length <= 65536, "metadata bounded");
        for (int ccsid : new int[]{500, 1140}) {
            Fake alternative = new Fake(); alternative.ccsid = ccsid;
            var other = ReadOnlyFormatIdentity.collect(alternative::call, ccsid, request(),
                    ReadOnlyFormatIdentity.Limits.defaults(), () -> 0L, () -> NOW);
            equal(LEVEL, other.header().levelId());
        }
    }

    private static void inputBounds() throws Exception {
        for (String name : List.of("*FIRST", "*LIBL", "items", "A".repeat(11), "ITEMS;DROP", "")) {
            invalid(() -> new ReadOnlyFormatIdentity.Request(name, "ITEMS", "ITEMSFMT", SHA));
            invalid(() -> new ReadOnlyFormatIdentity.Request("TESTLIB", name, "ITEMSFMT", SHA));
            invalid(() -> new ReadOnlyFormatIdentity.Request("TESTLIB", "ITEMS", name, SHA));
        }
        invalid(() -> new ReadOnlyFormatIdentity.Request("TESTLIB", "ITEMS", "ITEMSFMT", "A".repeat(64)));
        for (int[] values : List.of(new int[]{255, 65536, 60}, new int[]{65537, 65536, 60},
                new int[]{16384, 0, 60}, new int[]{16384, 262145, 60}, new int[]{16384, 65536, 0},
                new int[]{16384, 65536, 61})) {
            invalid(() -> new ReadOnlyFormatIdentity.Limits(values[0], values[1], values[2]));
        }
        for (int ccsid : new int[]{0, -1, 65535, 9999, 1208}) {
            Fake fake = new Fake();
            failure(() -> ReadOnlyFormatIdentity.collect(fake::call, ccsid, request(),
                    ReadOnlyFormatIdentity.Limits.defaults()), "unsupported_ccsid");
            equal(0, fake.calls);
        }
        invalid(() -> ReadOnlyFormatIdentity.program(null));
        invalid(() -> ReadOnlyFormatIdentity.program(new AS400()));
        invalid(() -> new ReadOnlyFormatIdentity.NativeReply(true, new byte[65537], new byte[20], error()));
        invalid(() -> new ReadOnlyFormatIdentity.NativeReply(true, new byte[256], new byte[21], error()));
        invalid(() -> new ReadOnlyFormatIdentity.NativeReply(true, new byte[256], new byte[20], new byte[17]));
    }

    private static void malformedReplies() throws Exception {
        for (String fault : List.of("null_reply", "native_false", "null_receiver", "short_allocation", "oversized_allocation",
                "returned_short", "returned_large", "returned_negative", "available_large", "available_short",
                "record_zero", "record_negative", "format_wrong", "format_lower", "level_blank", "level_printing_ten",
                "level_control", "level_high", "target_null", "target_short", "target_file", "target_library",
                "target_padding", "error_null", "error_short", "error_available", "error_provided", "error_id",
                "field_count_zero", "field_count_large", "field_length_zero", "field_length_large")) {
            Fake fake = new Fake(); fake.fault = fault;
            failure(() -> collect(fake), null);
            equal(1, fake.calls);
        }
    }

    private static void deadlinesAndFailures() throws Exception {
        Fake late = new Fake(); late.fault = "late";
        failure(() -> collect(late), "deadline_exceeded");
        Fake thrown = new Fake(); thrown.fault = "throw";
        failure(() -> collect(thrown), "identity_unavailable");
        Fake tooSmall = new Fake();
        failure(() -> ReadOnlyFormatIdentity.collect(tooSmall::call, CCSID, request(),
                new ReadOnlyFormatIdentity.Limits(16384, 100, 60), () -> 0L, () -> NOW), "metadata_budget_exceeded");
        Fake backwards = new Fake();
        long[] clock = {0};
        failure(() -> ReadOnlyFormatIdentity.collect(backwards::call, CCSID, request(),
                ReadOnlyFormatIdentity.Limits.defaults(), () -> --clock[0], () -> NOW), "deadline_exceeded");
        Instant[] time = {NOW};
        failure(() -> ReadOnlyFormatIdentity.collect(new Fake()::call, CCSID, request(),
                ReadOnlyFormatIdentity.Limits.defaults(), () -> 0L, () -> {
                    Instant result = time[0]; time[0] = NOW.minusSeconds(1); return result;
                }), "clock_invalid");
        long[] count = {0};
        Fake never = new Fake();
        failure(() -> ReadOnlyFormatIdentity.collect(never::call, CCSID, request(),
                ReadOnlyFormatIdentity.Limits.defaults(), () -> ++count[0] * 59_500_000_000L, () -> NOW), "deadline_exceeded");
        equal(0, never.calls);
    }

    private static void defensiveBuffers() throws Exception {
        Fake fake = new Fake(); var result = collect(fake);
        byte[] receiver = result.original().receiver(); receiver[80] = 0;
        check(result.original().receiver()[80] != 0, "receiver getter defensive");
        byte[] target = result.original().qualifiedTarget(); target[0] = 0;
        check(result.original().qualifiedTarget()[0] != 0, "target getter defensive");
        byte[] apiError = result.original().errorCode(); apiError[0] = 1;
        equal(0, (int) result.original().errorCode()[0]);
        byte[] layout = result.rawLayout(); layout[0] = 127;
        check(result.rawLayout()[0] != 127, "layout getter defensive");
        byte[] metadata = result.metadata(); metadata[0] = 0;
        equal((int) '{', (int) result.metadata()[0]);
        byte[] input = raw(CCSID, 16384); byte[] outTarget = target(CCSID); byte[] code = error();
        var reply = new ReadOnlyFormatIdentity.NativeReply(true, input, outTarget, code);
        input[80] = 0; outTarget[0] = 0; code[0] = 1;
        check(reply.receiver()[80] != 0 && reply.qualifiedTarget()[0] != 0 && reply.errorCode()[0] == 0,
                "reply constructor defensive");
        byte[] output = reply.receiver(); output[80] = 0;
        check(reply.receiver()[80] != 0, "reply getter defensive");
        List<byte[]> parameters = fake.request.parameters(); parameters.get(3)[0] = 0;
        equal("FILD0200", text(fake.request.parameters().get(3)));
        try { parameters.add(new byte[1]); throw new AssertionError("mutable parameter list"); }
        catch (UnsupportedOperationException expected) { assertions++; }
    }

    private static final class Fake {
        String fault = "";
        int calls, timeout, ccsid = CCSID;
        long nanos;
        ReadOnlyFormatIdentity.ProgramRequest request;
        ReadOnlyFormatIdentity.NativeReply call(ReadOnlyFormatIdentity.ProgramRequest call, int seconds) {
            calls++; request = call; timeout = seconds;
            if (fault.equals("throw")) throw new IllegalStateException("CANARY_LOCAL_DETAIL");
            if (fault.equals("null_reply")) return null;
            byte[] raw = raw(ccsid, call.receiverLength()), target = target(ccsid), code = error();
            switch (fault) {
                case "null_receiver" -> raw = null;
                case "short_allocation" -> raw = Arrays.copyOf(raw, raw.length - 1);
                case "oversized_allocation" -> raw = Arrays.copyOf(raw, raw.length + 1);
                case "returned_short" -> integer(raw, 0, 255);
                case "returned_large" -> integer(raw, 0, raw.length + 1);
                case "returned_negative" -> integer(raw, 0, -1);
                case "available_large" -> integer(raw, 4, RETURNED + 1);
                case "available_short" -> integer(raw, 4, RETURNED - 1);
                case "record_zero" -> integer(raw, 66, 0);
                case "record_negative" -> integer(raw, 66, -1);
                case "format_wrong" -> field(raw, 70, 10, "OTHER", ccsid);
                case "format_lower" -> field(raw, 70, 10, "itemsfmt", ccsid);
                case "level_blank" -> field(raw, 80, 13, "", ccsid);
                case "level_printing_ten" -> field(raw, 80, 13, "PRINTINGID", ccsid);
                case "level_control" -> raw[80] = 0;
                case "level_high" -> raw[80] = (byte) 255;
                case "target_null" -> target = null;
                case "target_short" -> target = Arrays.copyOf(target, 19);
                case "target_file" -> field(target, 0, 10, "OTHER", ccsid);
                case "target_library" -> field(target, 10, 10, "OTHER", ccsid);
                case "target_padding" -> target[9] = 0;
                case "error_null" -> code = null;
                case "error_short" -> code = Arrays.copyOf(code, 15);
                case "error_available" -> integer(code, 4, 32);
                case "error_provided" -> integer(code, 0, 0);
                case "error_id" -> code[8] = 1;
                case "field_count_zero" -> ByteBuffer.wrap(raw, 143, 2).putShort((short) 0);
                case "field_count_large" -> ByteBuffer.wrap(raw, 143, 2).putShort(Short.MAX_VALUE);
                case "field_length_zero" -> integer(raw, 256, 0);
                case "field_length_large" -> integer(raw, 256, raw.length);
                case "late" -> nanos = 61_000_000_000L;
                default -> { }
            }
            return new ReadOnlyFormatIdentity.NativeReply(!fault.equals("native_false"), raw, target, code);
        }
    }

    private static byte[] raw(int ccsid, int size) {
        byte[] result = new byte[size]; integer(result, 0, RETURNED); integer(result, 4, RETURNED);
        integer(result, 66, 37); field(result, 70, 10, "ITEMSFMT", ccsid); field(result, 80, 13, LEVEL, ccsid);
        ByteBuffer.wrap(result, 143, 2).putShort((short) 2);
        integer(result, 256, 112); integer(result, 368, 112);
        byte[] tail = HexFormat.of().parseHex("cafebabefeedbeef");
        System.arraycopy(tail, 0, result, size - tail.length, tail.length);
        return result;
    }
    private static byte[] target(int ccsid) { return new AS400Text(20, ccsid).toBytes("ITEMS     TESTLIB   "); }
    private static byte[] error() { return ByteBuffer.allocate(16).putInt(16).array(); }
    private static void integer(byte[] value, int offset, int number) { ByteBuffer.wrap(value, offset, 4).putInt(number); }
    private static void field(byte[] value, int offset, int width, String text, int ccsid) {
        System.arraycopy(new AS400Text(width, ccsid).toBytes(text), 0, value, offset, width);
    }
    private static String text(byte[] value) { return (String) new AS400Text(value.length, CCSID).toObject(value); }
    private static String hash(byte[] value) throws Exception { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value)); }
    @FunctionalInterface private interface Checked { void run() throws Exception; }
    private static void invalid(Checked action) throws Exception {
        try { action.run(); throw new AssertionError("invalid input accepted"); }
        catch (IllegalArgumentException expected) { assertions++; }
    }
    private static void failure(Checked action, String expectedCode) throws Exception {
        try { action.run(); throw new AssertionError("invalid native observation accepted"); }
        catch (ReadOnlyFormatIdentity.IdentityFailure failure) {
            if (expectedCode != null) equal(expectedCode, failure.code());
            check(!failure.getMessage().contains("CANARY"), "failure sanitized");
        }
    }
    private static void equal(Object expected, Object actual) { check(expected.equals(actual), "expected " + expected + " but got " + actual); }
    private static void check(boolean condition, String message) {
        assertions++; if (!condition) throw new AssertionError(message);
    }
}
