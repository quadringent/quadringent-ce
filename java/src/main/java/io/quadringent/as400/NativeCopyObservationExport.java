package io.quadringent.as400;

import com.ibm.as400.access.AS400JDBCConnection;
import com.ibm.as400.access.AS400Text;
import com.ibm.as400.access.Job;
import com.ibm.as400.access.ProgramCall;
import com.ibm.as400.access.ProgramParameter;
import com.ibm.as400.access.SecureAS400;
import com.ibm.as400.access.SocketProperties;
import com.ibm.as400.access.Trace;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.Reader;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Properties;
import java.util.Set;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/** Copy-time native observations. Equal endpoints do not establish historical continuity. */
public final class NativeCopyObservationExport {
    private static final Pattern NAME = Pattern.compile("[A-Z0-9_$#@]{1,10}");
    private static final Pattern SHA = Pattern.compile("[0-9a-f]{64}");
    private static final int NATIVE_BYTES = 65536;
    private NativeCopyObservationExport() {
    }
    public record JdbcJob(String name, String user, String number) {
        public JdbcJob {
            nativeName(name);
            nativeName(user);
            if (number == null || !number.matches("[0-9]{6}")) {
                throw new IllegalArgumentException("invalid_job");
            }
        }
    }
    public record JobState(String name, String user, String number, int ccsid) {
        public JobState {
            new JdbcJob(name, user, number);
            supportedCcsid(ccsid);
        }
    }
    public record JobPair(JobState before, JobState after) {
        public JobPair {
            if (before == null || after == null) {
                throw new IllegalArgumentException("invalid_job");
            }
        }
    }
    public record Request(String phase, String contextSha256, String copyRunId, String bindingSha256, String boundarySha256,
        String boundaryLibrary, String boundaryReceiver, BigInteger boundarySequence, String journalLibrary, String journalName,
        String receiverLibrary, String receiverName, BigInteger start, BigInteger end, String fileLibrary, String fileName,
        String memberName, String formatName, int maxMetadataBytes, int maxSeconds) {
        public Request {
            if (!Set.of("before", "after").contains(phase) || !hash(contextSha256) || !hash(bindingSha256) || !hash(boundarySha256)
                || copyRunId == null || !copyRunId.matches("[A-Za-z0-9_.-]{1,256}") || maxMetadataBytes<1 || maxMetadataBytes>1048576
                || maxSeconds<1 || maxSeconds>60) {
                throw new IllegalArgumentException("invalid_observation_scope");
            }
            for (String name : new String[] {
                boundaryLibrary, boundaryReceiver, journalLibrary, journalName, receiverLibrary, receiverName, fileLibrary,
                    fileName, memberName, formatName
            }) {
                nativeName(name);
            }
            validSequence(boundarySequence);
            validSequence(start);
            validSequence(end);
            if (end.compareTo(start)<0) {
                throw new IllegalArgumentException("invalid_witness_window");
            }
        }
        ReadOnlyMemberIdentity.Request memberRequest() {
            return new ReadOnlyMemberIdentity.Request(journalLibrary, journalName, receiverLibrary, receiverName,
                fileLibrary, fileName, memberName, start, end, contextSha256);
        }
    }
    public static final class ObservationFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private final String code;
        private ObservationFailure(String code) {
            super("native_copy_observation_error=" + code);
            this.code = code;
        }
        public String code() {
            return code;
        }
    }
    interface Session extends AutoCloseable {
        int ccsid();
        JdbcJob jdbcJob();
        Connection connection();
        ReadOnlyFormatIdentity.NativeReply format(ReadOnlyFormatIdentity.ProgramRequest request, int seconds) throws Exception;
        ReadOnlyFormatIdentity.NativeReply file(String library, String file, int seconds) throws Exception;
        ReadOnlyFormatIdentity.NativeReply rcac(String library, String file, String format, int seconds) throws Exception;
        ReadOnlyMemberIdentity.NativeReply member(ReadOnlyMemberIdentity.ProgramRequest request, int seconds) throws Exception;
        List<JobPair> jobs();
        NativeJournalPrefixForce.Result prefix(NativeJournalPrefixForce.Target target, int seconds) throws Exception;
        @Override void close() throws Exception;
    }
    @FunctionalInterface interface SessionFactory {
        Session open(String host, String user, String password, int seconds) throws Exception;
    }
    @FunctionalInterface interface JobReader {
        JobState read() throws Exception;
    }
    public static final class Result {
        private final byte[] metadata;
        private final NativeBusinessImageExport.Layout layout;
        private final String jidHex;
        private Result(byte[] metadata, NativeBusinessImageExport.Layout layout, String jidHex) {
            this.metadata = metadata.clone();
            this.layout = layout;
            this.jidHex = jidHex;
        }
        public String jidHex() {
            return jidHex;
        }
        public byte[] metadata() {
            return metadata.clone();
        }
        public NativeBusinessImageExport.Layout layout() {
            return layout;
        }
    }
    public static void main(String[] args) {
        int status = run(args, System.in, System.out, System.err, NativeSession::new, System::nanoTime, Instant::now);
        if (status != 0) {
            System.exit(status);
        }
    }
    static Request arguments(String[] a) {
        if (a == null || a.length != 20) {
            throw new IllegalArgumentException("invalid_arguments");
        }
        return new Request(a[0], a[1], a[2], a[3], a[4], a[5], a[6], sequence(a[7]), a[8], a[9], a[10], a[11], sequence(a[12]),
            sequence(a[13]), a[14], a[15], a[16], a[17], Integer.parseInt(a[18]), Integer.parseInt(a[19]));
    }
    static int run(String[] args, InputStream input, OutputStream output, OutputStream errors, SessionFactory factory,
        LongSupplier monotonic, Supplier<Instant> now) {
        try {
            Request request = arguments(args);
            Budget budget = new Budget(request.maxSeconds(), monotonic);
            Reader reader = new InputStreamReader(input, StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT));
            String host = line(reader, 253, budget), user = line(reader, 128, budget), password = line(reader, 4096, budget);
            if (!host.matches("[A-Za-z0-9][A-Za-z0-9.-]{0,252}") || !user.matches("[A-Za-z0-9_$#@]{1,128}") || password.isBlank()
                || reader.read() != -1) {
                throw new ObservationFailure("invalid_credentials");
            }
            requireTls();
            Result result;
            budget.seconds();
            try (Session session = factory.open(host, user, password, budget.seconds())) {
                result = collect(session, request, budget, now);
            }
            budget.seconds();
            output.write(result.metadata());
            output.flush();
            budget.seconds();
            if (output instanceof PrintStream p && p.checkError()) {
                throw new IOException();
            }
            return 0;
        } catch (Exception failure) {
            String code = failure instanceof ObservationFailure e ? e.code() : failure instanceof ReadOnlyMemberIdentity.IdentityFailure e ? e.code() : failure instanceof ReadOnlyFormatIdentity.IdentityFailure e ? e.code() : failure instanceof NativeBusinessImageExport.ExportFailure e ? e.code() : failure instanceof IllegalArgumentException ? "invalid_input" : "observation_unavailable";
            try {
                errors.write(("native_copy_observation_error=" + code + "\n").getBytes(StandardCharsets.US_ASCII));
                errors.flush();
            } catch (Exception ignored) {
            }
            return 2;
        }
    }
    static Result collect(Session session, Request request, Budget budget, Supplier<Instant> now) throws Exception {
        int jobStart = session.jobs().size();
        budget.seconds();
        int ccsid = session.ccsid();
        supportedCcsid(ccsid);
        Instant started = utc(now);
        Connection connection = session.connection();
        connection.setReadOnly(true);
        JdbcJob jdbcJob = session.jdbcJob();
        var file = session.file(request.fileLibrary(), request.fileName(), Math.min(60, budget.seconds()));
        if (!file.succeeded()) {
            throw new ObservationFailure("native_program_failed");
        }
        byte[] fileRaw = complete(file.receiver(), NATIVE_BYTES, 49);
        var format = ReadOnlyFormatIdentity.collect(session::format, ccsid, new ReadOnlyFormatIdentity.Request(request.fileLibrary(),
            request.fileName(), request.formatName(), request.contextSha256()), new ReadOnlyFormatIdentity.Limits(NATIVE_BYTES,
            262144, Math.min(60, budget.seconds())));
        budget.seconds();
        var rcac = session.rcac(request.fileLibrary(), request.fileName(), request.formatName(), Math.min(60, budget.seconds()));
        byte[] rcacRaw = complete(rcac.receiver(), NATIVE_BYTES, 112);
        var member = ReadOnlyMemberIdentity.collect(connection, session::member, ccsid, request.memberRequest(), new ReadOnlyMemberIdentity.Limits(65536,
            Math.min(60, budget.seconds())));
        budget.seconds();
        List<NativeBusinessImageExport.Column> columns = NativeBusinessImageExport.columns(connection, request.fileLibrary(),
            request.fileName(), budget.seconds());
        var nativeRequest = new NativeBusinessImageExport.Request(request.journalLibrary(), request.journalName(),
            request.receiverLibrary(), request.receiverName(), request.fileLibrary(), request.fileName(), request.memberName(),
            member.jidHex(), request.start(), request.end(), request.contextSha256(), request.copyRunId(), request.bindingSha256(),
            request.formatName(), columns);
        if (!rcac.succeeded()) {
            throw new ObservationFailure("native_program_failed");
        }
        var description = new NativeBusinessImageExport.NativeDescription(fileRaw, file.qualifiedTarget(), file.errorCode(),
            format.original().receiver(), format.original() .qualifiedTarget(), format.original().errorCode(), rcacRaw,
            rcac.qualifiedTarget(), rcac.errorCode(), ccsid, ccsid, ccsid);
        var layout = NativeBusinessImageExport.Layout.parse(description, nativeRequest);
        NativeBusinessImageExport.validateCatalogue(connection, nativeRequest, layout, budget.seconds());
        List<JobPair> jobs = List.copyOf(session.jobs().subList(jobStart, session.jobs().size()));
        if (jobs.size() != 7) {
            throw new ObservationFailure("native_job_unavailable");
        }
        JobState initial = jobs.getFirst().before();
        for (JobPair job : jobs) {
            if (!job.before().equals(initial) || !job.after().equals(initial) || initial.ccsid() != ccsid) {
                throw new ObservationFailure("native_job_changed");
            }
        }
        budget.seconds();
        Instant completed = utc(now);
        if (completed.isBefore(started)) {
            throw new ObservationFailure("source_clock");
        }
        String json = metadata(request, jdbcJob, file, fileRaw, format, rcac, rcacRaw, member, layout, columns, jobs,
            started, completed);
        byte[] data =(json + "\n").getBytes(StandardCharsets.UTF_8);
        if (data.length>request.maxMetadataBytes()) {
            throw new ObservationFailure("metadata_budget");
        }
        budget.seconds();
        return new Result(data, layout, member.jidHex());
    }
    static byte[] complete(byte[] raw, int allocated, int minimum) throws ObservationFailure {
        if (raw == null || raw.length != allocated || raw.length<8) {
            throw new ObservationFailure("native_reply_invalid");
        }
        int returned = ByteBuffer.wrap(raw).getInt(), available = ByteBuffer.wrap(raw, 4, 4).getInt();
        if (returned<minimum || returned>raw.length || returned != available) {
            throw new ObservationFailure("native_reply_invalid");
        }
        return Arrays.copyOf(raw, returned);
    }
    private static String metadata(Request r, JdbcJob jdbc, ReadOnlyFormatIdentity.NativeReply file, byte[] fileRaw,
        ReadOnlyFormatIdentity.Result f, ReadOnlyFormatIdentity.NativeReply rcac, byte[] rcacRaw,
        ReadOnlyMemberIdentity.Result member, NativeBusinessImageExport.Layout layout, List<NativeBusinessImageExport.Column> columns,
        List<JobPair> jobs, Instant start, Instant end) throws Exception {
        StringBuilder b = new StringBuilder("{\"format_version\":\"quadringent-native-copy-observation-v2\",\"phase\":")
            .append(q(r.phase())) .append(",\"context_sha256\":").append(q(r.contextSha256())).append(",\"copy_run_id\":")
            .append(q(r.copyRunId())) .append(",\"capture_binding_sha256\":").append(q(r.bindingSha256())).append(",\"copy_boundary_sha256\":")
            .append(q(r.boundarySha256())) .append(",\"copy_boundary\":{\"receiver_library\":").append(q(r.boundaryLibrary()))
            .append(",\"receiver_name\":").append(q(r.boundaryReceiver())).append(",\"sequence\":").append(q(r.boundarySequence()
            .toString())).append('}') .append(",\"journal\":{\"library\":").append(q(r.journalLibrary())).append(",\"name\":")
            .append(q(r.journalName())).append('}') .append(",\"witness_window\":{\"receiver_library\":").append(q(r.receiverLibrary()))
            .append(",\"receiver_name\":").append(q(r.receiverName())).append(",\"start_sequence\":").append(q(r.start().toString()))
            .append(",\"end_sequence\":").append(q(r.end().toString())).append('}') .append(",\"source_library\":").append(q(r.fileLibrary()))
            .append(",\"source_file\":").append(q(r.fileName())).append(",\"source_member\":").append(q(r.memberName())).append(",\"format_name\":")
            .append(q(r.formatName())) .append(",\"started_at\":").append(q(start.toString())).append(",\"completed_at\":")
            .append(q(end.toString())).append(",\"admission\":\"not_evaluated\",\"jdbc_job\":").append(jdbc(jdbc)) .append(",\"file\":{\"original_id\":\"file\",\"flags_8\":")
            .append(Byte.toUnsignedInt(fileRaw[8])) .append(",\"flags_23\":").append(Byte.toUnsignedInt(fileRaw[23])).append(",\"access_control_defined\":false}")
            .append(",\"format\":{\"level_id\":") .append(q(layout.levelId())).append(",\"record_length\":").append(layout.recordLength())
            .append(",\"layout_sha256\":") .append(q(layout.sha256())).append(",\"fields\":[");
        for (int n = 0;n<layout.fields().size();n++) {
            if (n>0) {
                b.append(',');
            }
            var field = layout.fields().get(n);
            var col = columns.get(n);
            b.append("{\"system_name\":").append(q(field.systemName())).append(",\"sql_name\":").append(q(col.name())).append(",\"kind\":")
                .append(q(col.kind())).append(",\"native_type\":").append(q(String.format(Locale.ROOT, "%04x", field.type())))
                .append(",\"input_offset\":").append(field.offset()).append(",\"output_offset\":").append(field.offset()).append(",\"byte_length\":")
                .append(field.byteLength()).append(",\"digits\":").append(field.digits()).append(",\"scale\":").append(field.scale())
                .append(",\"ccsid\":").append(field.ccsid()).append(",\"allocated_length\":").append(field.allocatedLength()).append('}');
        }
        b.append("],\"original_id\":\"format\"},\"rcac\":{\"row_permissions_state\":") .append(q(new AS400Text(1, jobs.get(2)
            .before().ccsid()).toObject(new byte[] {
            rcacRaw[21]
        }).toString())) .append(",\"column_masks_state\":") .append(q(new AS400Text(1, jobs.get(2).before().ccsid()).toObject(new byte[] {
            rcacRaw[22]
        }).toString())) .append(",\"row_permission_count\":0,\"column_mask_count\":0,\"original_id\":\"rcac\"," + "\"basis\":\"no_controls_defined\"}")
            .append(",\"member\":{\"jid_hex\":").append(q(member.jidHex())).append(",\"witnesses\":[");
        for (int n = 0;n<member.witnesses().size();n++) {
            if (n>0) {
                b.append(',');
            }
            var w = member.witnesses().get(n);
            b.append("{\"receiver_library\":").append(q(w.receiverLibrary())).append(",\"receiver_name\":").append(q(w.receiverName()))
                .append(",\"sequence\":").append(q(w.sequence())).append(",\"journal_code\":").append(q(w.code())).append(",\"journal_entry_type\":")
                .append(q(w.entryType())).append(",\"object\":").append(q(w.object())).append(",\"object_type\":").append(q(w.objectType()))
                .append('}');
        }
        b.append("],\"original_ids\":[\"member-before\",\"jid-first\",\"jid-last\",\"member-after\"]},\"originals\":[")
            .append(original("file", "QDBRTVFD", "FILD0100", fileRaw, file.qualifiedTarget(), file.errorCode(), jobs.get(0)))
            .append(',').append(original("format", "QDBRTVFD", "FILD0200", f.original().receiver(), f.original().qualifiedTarget(),
            f.original() .errorCode(), jobs.get(1))) .append(',').append(original("rcac", "QDBRTVFD", "FILD0500", rcacRaw,
            rcac.qualifiedTarget(), rcac.errorCode(), jobs.get(2)));
        String[] ids = {
            "member-before", "jid-first", "jid-last", "member-after"
        };
        for (int n = 0;n<4;n++) {
            var o = member.originals().get(n);
            b.append(',').append(original(ids[n], o.program(), o.format(), o.receiver(), null, o.errorCode(), jobs.get(n + 3)));
        }
        return b.append("]}").toString();
    }
    private static String original(String id, String program, String format, byte[] raw, byte[] target, byte[] error,
        JobPair job) throws Exception {
        return "{\"id\":" + q(id) + ",\"program\":" + q(program) + ",\"format\":" + q(format) + ",\"ccsid\":" + job.before()
            .ccsid() + ",\"job_before\":" + job(job.before()) + ",\"job_after\":" + job(job.after()) + ",\"receiver_base64\":"
            + q(Base64.getEncoder() .encodeToString(raw)) + ",\"receiver_sha256\":" + q(sha(raw)) + ",\"receiver_bytes\":"
            + raw.length + ",\"qualified_target_base64\":" +(target == null ? "null" : q(Base64.getEncoder().encodeToString(target)))
            + ",\"qualified_target_sha256\":" +(target == null ? "null" : q(sha(target))) + ",\"error_base64\":" + q(Base64.getEncoder()
            .encodeToString(error)) + ",\"error_sha256\":" + q(sha(error)) + "}";
    }
    static String q(String value) throws Exception {
        StringBuilder b = new StringBuilder("\"");
        for (int n = 0;n<value.length();n++) {
            char c = value.charAt(n);
            if (Character.isSurrogate(c)) {
                throw new IllegalArgumentException("invalid_text");
            }
            switch (c) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                default -> {
                    if (c<32) {
                        b.append(String.format(Locale.ROOT, "\\u%04x",(int)c));
                    } else b.append(c);
                }
            }
        }
        return b.append('"').toString();
    }
    private static String jdbc(JdbcJob j) throws Exception {
        return "{\"name\":" + q(j.name()) + ",\"user\":" + q(j.user()) + ",\"number\":" + q(j.number()) + "}";
    }
    private static String job(JobState j) throws Exception {
        return "{\"name\":" + q(j.name()) + ",\"user\":" + q(j.user()) + ",\"number\":" + q(j.number()) + ",\"ccsid\":"
            + j.ccsid() + "}";
    }
    static String sha(byte[] raw) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
    }
    static void supportedCcsid(int ccsid) {
        if (!Set.of(37, 273, 367).contains(ccsid)) {
            throw new IllegalArgumentException("unsupported_ccsid");
        }
    }
    private static void nativeName(String name) {
        if (name == null || !NAME.matcher(name).matches()) {
            throw new IllegalArgumentException("invalid_native_name");
        }
    }
    private static boolean hash(String value) {
        return value != null && SHA.matcher(value).matches();
    }
    private static void validSequence(BigInteger n) {
        if (n == null || n.signum()<1 || n.toString().length()>21) {
            throw new IllegalArgumentException("invalid_sequence");
        }
    }
    private static BigInteger sequence(String s) {
        if (s == null || !s.matches("[1-9][0-9]{0,20}")) {
            throw new IllegalArgumentException("invalid_sequence");
        }
        return new BigInteger(s);
    }
    private static Instant utc(Supplier<Instant> now) throws ObservationFailure {
        Instant instant = now.get();
        if (instant == null || instant.atOffset(ZoneOffset.UTC).getYear()<1 || instant.atOffset(ZoneOffset.UTC).getYear()>9999) {
            throw new ObservationFailure("source_clock");
        }
        return instant;
    }
    static String line(Reader reader, int max, Budget budget) throws Exception {
        StringBuilder b = new StringBuilder();
        while (true) {
            budget.seconds();
            int c = reader.read();
            budget.seconds();
            if (c<0 || c == '\r' || c == 0) {
                throw new IllegalArgumentException("invalid_credentials");
            }
            if (c == '\n') {
                return b.toString();
            }
            if (b.length() == max) {
                throw new IllegalArgumentException("credential_budget");
            }
            b.append((char)c);
        }
    }
    static void requireTls() throws ObservationFailure {
        if (Trace.isTraceOn()) {
            throw new ObservationFailure("trace_disabled_required");
        }
        for (String key : List.of("com.ibm.as400.access.SSLTrustAll", "com.ibm.as400.access.SecureAS400.trustAll")) {
            String value = System.getProperty(key);
            if (value != null && Set.of("true", "1", "yes", "on").contains(value.strip().toLowerCase(Locale.ROOT))) {
                throw new ObservationFailure("tls_required");
            }
        }
    }
    static final class Budget {
        final LongSupplier clock;
        final long started;
        long previous;
        final int max;
        Budget(int max, LongSupplier clock) {
            this.max = max;
            this.clock = clock;
            started = previous = clock.getAsLong();
        }
        int seconds() throws ObservationFailure {
            long n = clock.getAsLong(), remaining =(long)max * 1000000000 -(n - started);
            if (n<previous || remaining <= 0) {
                throw new ObservationFailure("deadline");
            }
            previous = n;
            return Math.max(1,(int)(remaining / 1000000000));
        }
    }
    static final class NativeSession implements Session {
        final SecureAS400 system;
        final ProgramCall call;
        final Connection connection;
        final JobState initial;
        final JdbcJob jdbc;
        final JobReader jobReader;
        final List<JobPair> jobs = new ArrayList<>();
        NativeSession(SecureAS400 system, ProgramCall call, Connection connection, JobState initial, JdbcJob jdbc,
            JobReader jobReader) {
            this.system = system;
            this.call = call;
            this.connection = connection;
            this.initial = initial;
            this.jdbc = jdbc;
            this.jobReader = jobReader;
        }
        NativeSession(String host, String user, String password, int seconds) throws Exception {
            requireTls();
            TlsTrust.install(true);
            system = new SecureAS400(host, user, password.toCharArray());
            jobReader = this::freshJob;
            Connection opened = null;
            try {
                system.setGuiAvailable(false);
                system.setMustUseSockets(true);
                sockets(seconds);
                call = new ProgramCall(system);
                call.setThreadSafe(false);
                call.setProgram("/QSYS.LIB/QDBRTVFD.PGM");
                call.setTimeOut(seconds);
                initial = jobReader.read();
                Class.forName("com.ibm.as400.access.AS400JDBCDriver");
                Properties properties = NativeVerificationSourceExport.connectionProperties(user, password);
                properties.setProperty("socket timeout", Integer.toString(seconds * 1000));
                properties.setProperty("login timeout", Integer.toString(Math.min(15, seconds)));
                opened = DriverManager.getConnection("jdbc:as400://" + host, properties);
                connection = opened;
                if (!(connection instanceof AS400JDBCConnection nativeConnection)) {
                    throw new ObservationFailure("jdbc_job_unavailable");
                }
                String id = nativeConnection.getServerJobIdentifier();
                if (id == null || id.length() != 26) {
                    throw new ObservationFailure("jdbc_job_unavailable");
                }
                jdbc = new JdbcJob(id.substring(0, 10).stripTrailing(), id.substring(10, 20).stripTrailing(), id.substring(20,
                    26));
            } catch (Exception failure) {
                try {
                    if (opened != null) {
                        opened.close();
                    }
                } finally {
                    system.disconnectAllServices();
                }
                throw failure;
            }
        }
        private void sockets(int seconds) throws Exception {
            SocketProperties p = new SocketProperties();
            p.setSoTimeout(seconds * 1000);
            p.setLoginTimeout(seconds * 1000);
            system.setSocketProperties(p);
        }
        private JobState freshJob() throws Exception {
            Job known = call.getServerJob();
            if (known == null) {
                throw new ObservationFailure("native_job_unavailable");
            }
            Job fresh = new Job(system, known.getName(), known.getUser(), known.getNumber());
            return new JobState(fresh.getName(), fresh.getUser(), fresh.getNumber(), fresh.getCodedCharacterSetID());
        }
        boolean dispatch(String program, ProgramParameter[] params, int seconds) throws Exception {
            requireTls();
            // JTOpen freezes socket properties at connection; only the call timeout may decrease.
            call.setTimeOut(seconds);
            JobState before = jobReader.read();
            if (!before.equals(initial)) {
                throw new ObservationFailure("native_job_changed");
            }
            call.setProgram("/QSYS.LIB/" + program + ".PGM", params);
            boolean result = call.run();
            JobState after = jobReader.read();
            jobs.add(new JobPair(before, after));
            if (!before.equals(after)) {
                throw new ObservationFailure("native_job_changed");
            }
            return result;
        }
        @Override public NativeJournalPrefixForce.Result prefix(NativeJournalPrefixForce.Target target, int seconds) throws Exception {
            return NativeJournalPrefixForce.force(this, target, seconds);
        }
        @Override public int ccsid() {
            return initial.ccsid();
        }
        @Override public Connection connection() {
            return connection;
        }
        @Override public JdbcJob jdbcJob() {
            return jdbc;
        }
        @Override public List<JobPair> jobs() {
            return List.copyOf(jobs);
        }
        @Override public ReadOnlyFormatIdentity.NativeReply format(ReadOnlyFormatIdentity.ProgramRequest request, int seconds) throws Exception {
            List<byte[]> inputs = request.parameters();
            ProgramParameter[] p = new ProgramParameter[10];
            for (int n = 0;n<10;n++) {
                p[n] = n == 0 ? new ProgramParameter(request.receiverLength()) : n == 2 ? new ProgramParameter(20) : n == 9 ? new ProgramParameter(inputs.get(n),
                    16) : new ProgramParameter(inputs.get(n));
            }
            boolean result = dispatch("QDBRTVFD", p, seconds);
            return new ReadOnlyFormatIdentity.NativeReply(result, p[0].getOutputData(), p[2].getOutputData(), p[9].getOutputData());
        }
        @Override public ReadOnlyFormatIdentity.NativeReply file(String library, String file, int seconds) throws Exception {
            return description("FILD0100", library, file, "*FIRST", seconds);
        }
        @Override public ReadOnlyFormatIdentity.NativeReply rcac(String library, String file, String format, int seconds) throws Exception {
            return description("FILD0500", library, file, format, seconds);
        }
        private ReadOnlyFormatIdentity.NativeReply description(String replyFormat, String library, String file, String selector,
            int seconds) throws Exception {
            var inputs = List.of(new byte[0], bin(NATIVE_BYTES), new byte[0], text(replyFormat, 8), text(pad(file) + pad(library),
                20), text(selector, 10), text("0", 1), text("*LCL", 10), text("*EXT", 10), binError());
            ProgramParameter[] p = new ProgramParameter[10];
            for (int n = 0;n<10;n++) {
                p[n] = n == 0 ? new ProgramParameter(NATIVE_BYTES) : n == 2 ? new ProgramParameter(20) : n == 9 ? new ProgramParameter(inputs.get(n),
                    16) : new ProgramParameter(inputs.get(n));
            }
            boolean result = dispatch("QDBRTVFD", p, seconds);
            return new ReadOnlyFormatIdentity.NativeReply(result, p[0].getOutputData(), p[2].getOutputData(), p[9].getOutputData());
        }
        @Override public ReadOnlyMemberIdentity.NativeReply member(ReadOnlyMemberIdentity.ProgramRequest request, int seconds) throws Exception {
            List<byte[]> inputs = request.parameters();
            ProgramParameter[] p = new ProgramParameter[inputs.size()];
            for (int n = 0;n<p.length;n++) {
                p[n] = n == 0 ? new ProgramParameter(request.receiverLength()) : n == request.errorIndex() ? new ProgramParameter(inputs.get(n),
                    16) : new ProgramParameter(inputs.get(n));
            }
            boolean result = dispatch(request.program(), p, seconds);
            return new ReadOnlyMemberIdentity.NativeReply(result, p[0].getOutputData(), p[request.errorIndex()].getOutputData());
        }
        private byte[] text(String text, int width) {
            return new AS400Text(width, initial.ccsid()).toBytes(text);
        }
        private static byte[] bin(int n) {
            return ByteBuffer.allocate(4).putInt(n).array();
        }
        private static byte[] binError() {
            return ByteBuffer.allocate(16).putInt(16).array();
        }
        private static String pad(String s) {
            return s + " ".repeat(10 - s.length());
        }
        @Override public void close() throws Exception {
            try {
                connection.close();
            } finally {
                system.disconnectAllServices();
            }
        }
    }
}
