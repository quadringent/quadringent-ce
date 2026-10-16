package io.quadringent.as400;

import com.ibm.as400.access.AS400Text;
import com.ibm.as400.access.AS400ZonedDecimal;
import com.ibm.as400.access.Job;
import com.ibm.as400.access.ProgramCall;
import com.ibm.as400.access.ProgramParameter;
import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Types;
import java.util.Arrays;
import java.util.Base64;
import java.util.List;
import java.util.Set;
import static io.quadringent.as400.NativeCopyObservationExport.q;

/** Forces only existing journal data. A durable prefix is not a transaction cut. */
public final class NativeJournalPrefixForce {
    private static final String PATH = "/QSYS.LIB/QJOSJRNE.PGM";
    private static final String SQL = "SELECT JOURNAL_LIBRARY,JOURNAL_NAME,JOURNAL_TYPE,JOURNAL_STATE FROM QSYS2.JOURNAL_INFO WHERE "
        + "JOURNAL_LIBRARY = ? AND JOURNAL_NAME = ? FETCH FIRST 2 ROWS ONLY";
    private NativeJournalPrefixForce() {
    }
    public record Target(String journalLibrary, String journalName) {
        public Target {
            for (String s : List.of(journalLibrary, journalName)) {
                if (!s.matches("[A-Z0-9_$#@]{1,10}")) {
                    throw new IllegalArgumentException("invalid_journal");
                }
            }
        }
    }
    public enum Mode {
        IN, OUT, INOUT
    }
    public record Parameter(Mode mode, byte[] input, int outputLength) {
        public Parameter {
            if (mode == null || input == null || input.length>64 || outputLength<0 || outputLength>58) {
                throw new IllegalArgumentException("invalid_parameter");
            }
            input = input.clone();
        }
        @Override public byte[] input() {
            return input.clone();
        }
    }
    public static final class Request {
        private final Target target;
        private final int ccsid;
        private final List<Parameter> parameters;
        private Request(Target target, int ccsid, List<Parameter> parameters) {
            this.target = target;
            this.ccsid = ccsid;
            this.parameters = List.copyOf(parameters);
        }
        public List<Parameter> parameters() {
            return parameters;
        }
    }
    public record Reply(boolean succeeded, byte[] receiver, byte[] errorCode) {
        public Reply {
            if (receiver != null && receiver.length>58 || errorCode != null && errorCode.length>16) {
                throw new IllegalArgumentException("oversized_native_reply");
            }
            receiver = receiver == null ? null : receiver.clone();
            errorCode = errorCode == null ? null : errorCode.clone();
        }
        @Override public byte[] receiver() {
            return receiver == null ? null : receiver.clone();
        }
        @Override public byte[] errorCode() {
            return errorCode == null ? null : errorCode.clone();
        }
    }
    public record Prefix(String sequence, String receiverLibrary, String receiverName, String aspDevice) {
    }
    public static final class ForceFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private ForceFailure(String code) {
            super("native_prefix_error=" + code);
            this.code = code;
        }
        public String code() {
            return code;
        }
    }
    public static final class Result {
        private final Target target;
        private final Request request;
        private final Prefix prefix;
        private final Reply original;
        private final NativeCopyObservationExport.JobPair job;
        private final NativeCopyObservationExport.JdbcJob sqlJob;
        private Result(Target target, Request request, Prefix prefix, Reply original, NativeCopyObservationExport.JobPair job,
            NativeCopyObservationExport.JdbcJob sqlJob) {
            this.target = target;
            this.request = request;
            this.prefix = prefix;
            this.original = original;
            this.job = job;
            this.sqlJob = sqlJob;
        }
        public Target target() {
            return target;
        }
        public Prefix prefix() {
            return prefix;
        }
        public byte[] receiver() {
            return original.receiver();
        }
        public byte[] errorCode() {
            return original.errorCode();
        }
        public String json() throws Exception {
            return "{\"journal_library\":" + q(target.journalLibrary()) + ",\"journal_name\":" + q(target.journalName()) + ",\"receiver_library\":"
                + q(prefix.receiverLibrary()) + ",\"receiver_name\":" + q(prefix.receiverName()) + ",\"sequence\":" + q(prefix.sequence())
                + ",\"asp_device\":" + q(prefix.aspDevice()) + ",\"program\":\"QJOSJRNE\",\"format\":\"SJNE0100\",\"force_key\":4,\"force_value\":\"2\","
                + "\"entry_data_length\":0,\"journal_type\":\"*LOCAL\",\"journal_state\":\"*ACTIVE\",\"sql_job\":" + jdbcJson(sqlJob)
                + ",\"request_parameters\":" + requestJson(request) + ",\"job_before\":" + jobJson(job.before()) + ",\"job_after\":"
                + jobJson(job.after()) + ",\"receiver_base64\":" + q(Base64.getEncoder().encodeToString(receiver())) + ",\"receiver_sha256\":"
                + q(NativeCopyObservationExport.sha(receiver())) + ",\"error_base64\":" + q(Base64.getEncoder() .encodeToString(errorCode()))
                + ",\"error_sha256\":" + q(NativeCopyObservationExport.sha(errorCode())) + ",\"transaction_cut\":false}";
        }
    }
    interface Transport {
        Connection connection();
        NativeCopyObservationExport.JdbcJob sqlJob();
        NativeCopyObservationExport.JobState job(int seconds) throws Exception;
        Reply force(Request request, int seconds) throws Exception;
    }
    public static Request prepare(Target target, int ccsid) throws ForceFailure {
        try {
            NativeCopyObservationExport.supportedCcsid(ccsid);
        } catch (Exception failure) {
            throw new ForceFailure("unsupported_ccsid");
        }
        if (target == null) {
            throw new ForceFailure("invalid_target");
        }
        byte[] info = ByteBuffer.allocate(13).putInt(1).putInt(4).putInt(1).put(text("2", 1, ccsid)[0]).array();
        return new Request(target, ccsid, List.of(in(text(pad(target.journalName()) + pad(target.journalLibrary()), 20,
            ccsid)), in(info), in(new byte[1]), in(bin(0)), new Parameter(Mode.INOUT, error(), 16), new Parameter(Mode.OUT,
            new byte[0], 58), in(bin(58)), in(text("SJNE0100", 8, ccsid)), in(bin(0))));
    }
    public static Prefix parse(Reply reply, int ccsid) throws ForceFailure {
        try {
            NativeCopyObservationExport.supportedCcsid(ccsid);
            if (reply == null || !reply.succeeded() || reply.errorCode() == null || reply.errorCode().length != 16 || integer(reply.errorCode(),
                0) != 16 || integer(reply.errorCode(), 4) != 0) {
                throw new ForceFailure("native_error");
            }
            byte[] raw = reply.receiver();
            if (raw == null || raw.length != 58 || integer(raw, 0) != 58 || integer(raw, 4) != 58) {
                throw new ForceFailure("native_output_header");
            }
            byte[] sequence = Arrays.copyOfRange(raw, 8, 28);
            for (int n = 0;n<20;n++) {
                int digit = sequence[n] & 15, zone =(sequence[n] & 255)>>>4;
                if (digit>9 ||(n<19 ? zone != 15 : !Set.of(10, 12, 14, 15).contains(zone))) {
                    throw new ForceFailure("unsupported_zoned_sequence");
                }
            }
            Object decoded = new AS400ZonedDecimal(20, 0).toObject(sequence);
            if (!(decoded instanceof BigDecimal decimal) || decimal.signum()<1) {
                throw new ForceFailure("unsupported_empty_prefix");
            }
            String exact = decimal.toBigIntegerExact().toString();
            String receiver = decode(raw, 28, ccsid), library = decode(raw, 38, ccsid), asp = decode(raw, 48, ccsid);
            if (!receiver.matches("[A-Z0-9_$#@]{1,10}") || !library.matches("[A-Z0-9_$#@]{1,10}") || !asp.matches("[ -~]{0,10}")) {
                throw new ForceFailure("native_receiver_invalid");
            }
            return new Prefix(exact, library, receiver, asp);
        } catch (ForceFailure failure) {
            throw failure;
        } catch (Exception failure) {
            throw new ForceFailure("native_output_invalid");
        }
    }
    static Result collect(Transport transport, Target target, int seconds) throws Exception {
        if (transport == null || seconds<1 || seconds>60) {
            throw new ForceFailure("invalid_input");
        }
        NativeCopyObservationExport.requireTls();
        journal(transport.connection(), target, seconds);
        var before = transport.job(seconds);
        Request request = prepare(target, before.ccsid());
        Reply reply = transport.force(request, seconds);
        var after = transport.job(seconds);
        if (!before.equals(after)) {
            throw new ForceFailure("native_job_changed");
        }
        return new Result(target, request, parse(reply, before.ccsid()), reply, new NativeCopyObservationExport.JobPair(before,
            after), transport.sqlJob());
    }
    static Result force(NativeCopyObservationExport.NativeSession session, Target target, int seconds) throws Exception {
        ProgramCall call = new ProgramCall(session.system);
        call.setThreadSafe(false);
        call.setProgram(PATH);
        call.setTimeOut(seconds);
        return collect(new Transport() {
            @Override public Connection connection() {
                return session.connection();
            }
            @Override public NativeCopyObservationExport.JdbcJob sqlJob() {
                return session.jdbcJob();
            }
            @Override public NativeCopyObservationExport.JobState job(int remaining) throws Exception {
                call.setTimeOut(remaining);
                Job known = call.getServerJob();
                if (known == null) {
                    throw new ForceFailure("native_job_unavailable");
                }
                Job fresh = new Job(session.system, known.getName(), known.getUser(), known.getNumber());
                return new NativeCopyObservationExport.JobState(fresh.getName(), fresh.getUser(), fresh.getNumber(), fresh.getCodedCharacterSetID());
            }
            @Override public Reply force(Request request, int remaining) throws Exception {
                ProgramParameter[] parameters = new ProgramParameter[9];
                for (int n = 0;n<9;n++) {
                    Parameter p = request.parameters.get(n);
                    parameters[n] = switch (p.mode()) {
                        case IN -> new ProgramParameter(p.input());
                        case OUT -> new ProgramParameter(p.outputLength());
                        case INOUT -> new ProgramParameter(p.input(), p.outputLength());
                    };
                }
                call.setTimeOut(remaining);
                call.setParameterList(parameters);
                boolean ok = call.run();
                return new Reply(ok, parameters[5].getOutputData(), parameters[4].getOutputData());
            }
        }, target, seconds);
    }
    private static void journal(Connection connection, Target target, int seconds) throws Exception {
        connection.setReadOnly(true);
        try (PreparedStatement s = connection.prepareStatement(SQL)) {
            s.setString(1, target.journalLibrary());
            s.setString(2, target.journalName());
            s.setQueryTimeout(seconds);
            try (var r = s.executeQuery()) {
                var m = r.getMetaData();
                String[] labels = {
                    "JOURNAL_LIBRARY", "JOURNAL_NAME", "JOURNAL_TYPE", "JOURNAL_STATE"
                };
                if (m == null || m.getColumnCount() != 4) {
                    throw new ForceFailure("journal_metadata_invalid");
                }
                for (int n = 1;n <= 4;n++) {
                    if (!labels[n -1].equals(m.getColumnLabel(n)) || m.getColumnType(n) != Types.VARCHAR || m.getPrecision(n) != 10) {
                        throw new ForceFailure("journal_metadata_invalid");
                    }
                }
                if (!r.next() || !target.journalLibrary().equals(r.getString(1)) || !target.journalName().equals(r.getString(2))
                    || !"*LOCAL".equals(r.getString(3)) || !"*ACTIVE".equals(r.getString(4)) || r.next()) {
                    throw new ForceFailure("journal_precondition");
                }
                if (r.getWarnings() != null || s.getWarnings() != null || connection.getWarnings() != null) {
                    throw new ForceFailure("journal_warning");
                }
            }
        }
    }
    private static String requestJson(Request request) throws Exception {
        StringBuilder result = new StringBuilder("[");
        for (int n = 0;n<request.parameters.size();n++) {
            if (n>0) {
                result.append(',');
            }
            Parameter p = request.parameters.get(n);
            byte[] raw = p.input();
            result.append("{\"mode\":").append(q(p.mode().name())).append(",\"output_length\":").append(p.outputLength()) .append(",\"input_base64\":")
                .append(q(Base64.getEncoder().encodeToString(raw))) .append(",\"input_sha256\":").append(q(NativeCopyObservationExport.sha(raw)))
                .append('}');
        }
        return result.append(']').toString();
    }
    private static String jdbcJson(NativeCopyObservationExport.JdbcJob j) throws Exception {
        return "{\"name\":" + q(j.name()) + ",\"user\":" + q(j.user()) + ",\"number\":" + q(j.number()) + "}";
    }
    private static String jobJson(NativeCopyObservationExport.JobState j) throws Exception {
        return "{\"name\":" + q(j.name()) + ",\"user\":" + q(j.user()) + ",\"number\":" + q(j.number()) + ",\"ccsid\":"
            + j.ccsid() + "}";
    }
    private static String pad(String s) {
        return s + " ".repeat(10 - s.length());
    }
    private static byte[] bin(int n) {
        return ByteBuffer.allocate(4).putInt(n).array();
    }
    private static byte[] error() {
        return binError();
    }
    private static byte[] binError() {
        return ByteBuffer.allocate(16).putInt(16).array();
    }
    private static Parameter in(byte[] b) {
        return new Parameter(Mode.IN, b, 0);
    }
    private static int integer(byte[] b, int offset) {
        return ByteBuffer.wrap(b, offset, 4).getInt();
    }
    private static byte[] text(String s, int width, int ccsid) {
        return new AS400Text(width, ccsid).toBytes(s);
    }
    private static String decode(byte[] raw, int offset, int ccsid) throws ForceFailure {
        Charset codec = Charset.forName(ccsid == 367 ? "US-ASCII" : ccsid == 37 ? "IBM037" : "IBM273");
        String value = new String(raw, offset, 10, codec);
        if (!Arrays.equals(Arrays.copyOfRange(raw, offset, offset + 10), value.getBytes(codec))) {
            throw new ForceFailure("native_text_invalid");
        }
        return value.replaceFirst(" +$", "");
    }
}
