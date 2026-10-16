package io.quadringent.as400;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.Reader;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.LinkOption;
import java.nio.file.OpenOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Set;
import static io.quadringent.as400.NativeCopyObservationExport.q;

/** Native producer orchestration. Its final guard covers this producer, not later consumers. */
final class NativeBusinessImageProducer {
    private static final int MAX_RECEIPT = 4 * 1024 * 1024;
    private NativeBusinessImageProducer() {
    }
    private record Command(NativeBusinessImageExport.Request request, String contextSha, String boundarySha, String boundaryLibrary,
        String boundaryReceiver, BigInteger boundary, Path bundle, String bundleSha, Path receipt,
        NativeBusinessImageExport.Limits limits) {
        static Command parse(String[] args) {
            if (args == null || args.length != 27) {
                throw new IllegalArgumentException("invalid_arguments");
            }
            var names = args[14].split(",", -1);
            var kinds = args[15].split(",", -1);
            if (names.length != kinds.length) {
                throw new IllegalArgumentException("invalid_columns");
            }
            var columns = new ArrayList<NativeBusinessImageExport.Column>();
            for (int n = 0; n < names.length; n++) {
                columns.add(new NativeBusinessImageExport.Column(names[n], kinds[n]));
            }
            var request = new NativeBusinessImageExport.Request(args[0], args[1], args[2], args[3], args[4], args[5], args[6],
                args[7], sequence(args[8]), sequence(args[9]), args[10], args[11], args[12], args[13], columns);
            if (!args[16].matches("[0-9a-f]{64}") || !args[17].matches("[0-9a-f]{64}") || !args[18].equals(request.receiverLibrary())
                || !args[19].equals(request.receiverName())) {
                throw new IllegalArgumentException("unsupported_receiver_scope");
            }
            BigInteger boundary = sequence(args[20]);
            if (boundary.signum() < 1 || !boundary.add(BigInteger.ONE).equals(request.start())) {
                throw new IllegalArgumentException("unsupported_boundary");
            }
            Path bundle = Path.of(args[21]), receipt = Path.of(args[23]);
            if (!bundle.isAbsolute() || !receipt.isAbsolute() || bundle.equals(receipt)) {
                throw new IllegalArgumentException("invalid_paths");
            }
            int seconds = Integer.parseInt(args[26]);
            var limits = new NativeBusinessImageExport.Limits(Long.parseLong(args[24]), Long.parseLong(args[25]), 1048576,
                1048576, seconds);
            return new Command(request, args[16], args[17], args[18], args[19], boundary, bundle, args[22], receipt, limits);
        }
    }
    private static final class State {
        NativeCopyObservationExport.Result before;
        NativeCopyObservationExport.Result after;
        NativeLifecycleGuard.Proof first;
        NativeLifecycleGuard.Proof last;
        String failedGuard;
        String failure;
        NativeBusinessImageExport.Layout copyLayout;
    }
    static int run(String[] args, InputStream input, OutputStream output, OutputStream errors,
        NativeCopyObservationExport.SessionFactory factory, boolean guardOnly) {
        Command command = null;
        State state = new State();
        Receipt receipt = null;
        try {
            command = Command.parse(args);
            NativeCopyBundle bundle = NativeCopyBundle.read(command.bundle(), command.bundleSha());
            state.copyLayout = bundle.validate(command.request());
            var budget = new NativeCopyObservationExport.Budget(command.limits().maxSeconds(), System::nanoTime);
            Reader reader = new InputStreamReader(input, StandardCharsets.UTF_8.newDecoder() .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT));
            String host = NativeCopyObservationExport.line(reader, 253, budget);
            String user = NativeCopyObservationExport.line(reader, 128, budget);
            String password = NativeCopyObservationExport.line(reader, 4096, budget);
            if (!host.matches("[A-Za-z0-9][A-Za-z0-9.-]{0,252}") || !user.matches("[A-Za-z0-9_$#@]{1,128}") || password.isBlank()
                || reader.read() != -1) {
                throw new IllegalArgumentException("invalid_credentials");
            }
            NativeCopyObservationExport.requireTls();
            receipt = new Receipt(command.receipt());
            final Command captured = command;
            final Receipt destination = receipt;
            byte[] guardDocument = null;
            try (var session = factory.open(host, user, password, seconds(budget))) {
                state.before = observation(session, command, "before", budget);
                match(command, state.copyLayout, state.before, session, budget);
                state.first = guard(session, command, state.before.layout(), budget);
                if (guardOnly) {
                    state.last = state.first;
                    guardDocument = document(command, state, "after_all_streams", true);
                    receipt.write(guardDocument, budget);
                } else {
                    var permit = NativeBusinessImageExport.guardedPermit(command.request(), state.before.layout(), state.first,
                        bundle, state.before.metadata());
                    NativeBusinessImageExport.export(session.connection(), command.request(), state.before.layout(), permit,
                        command.limits(), output,() -> {
                        try {
                            state.after = observation(session, captured, "after", budget);
                            match(captured, state.copyLayout, state.after, session, budget);
                            state.last = guard(session, captured, state.after.layout(), budget);
                            destination.write(document(captured, state, "journal", true), budget);
                        } catch (Exception failure) {
                            if (state.failure == null) {
                                state.failure = code(failure);
                            }
                            if (failure instanceof NativeLifecycleGuard.GuardFailure failed) {
                                state.failedGuard = failed.partialJson();
                            }
                            throw failure;
                        }
                    });
                }
            }
            budget.seconds();
            if (guardOnly) {
                output.write(guardDocument);
                output.flush();
                if (output instanceof PrintStream printed && printed.checkError()) {
                    throw new java.io.IOException();
                }
            }
            budget.seconds();
            return 0;
        } catch (Exception failure) {
            if (state.failure == null) {
                state.failure = code(failure);
            }
            if (failure instanceof NativeLifecycleGuard.GuardFailure failed) {
                state.failedGuard = failed.partialJson();
            }
            try {
                if (receipt != null && !receipt.closed && command != null) {
                    receipt.write(document(command, state, guardOnly ? "after_all_streams" : "journal", false), null);
                }
            } catch (Exception ignored) {
            }
            try {
                errors.write(("native_business_export_error=" + state.failure + "\n").getBytes(StandardCharsets.US_ASCII));
                errors.flush();
            } catch (Exception ignored) {
            }
            return 2;
        } finally {
            try {
                if (receipt != null) {
                    receipt.close();
                }
            } catch (Exception ignored) {
            }
        }
    }
    private static NativeCopyObservationExport.Result observation(NativeCopyObservationExport.Session session,
        Command c, String phase, NativeCopyObservationExport.Budget budget) throws Exception {
        var r = c.request();
        var request = new NativeCopyObservationExport.Request(phase, c.contextSha(), r.copyRunId(), r.captureBindingSha256(),
            c.boundarySha(), c.boundaryLibrary(), c.boundaryReceiver(), c.boundary(), r.journalLibrary(), r.journalName(),
            r.receiverLibrary(), r.receiverName(), BigInteger.ONE, r.end().max(c.boundary()), r.fileLibrary(), r.fileName(),
            r.member(), r.formatName(), 1048576, seconds(budget));
        return NativeCopyObservationExport.collect(session, request, budget, Instant::now);
    }
    private static void match(Command command, NativeBusinessImageExport.Layout copied, NativeCopyObservationExport.Result current,
        NativeCopyObservationExport.Session session, NativeCopyObservationExport.Budget budget) throws Exception {
        if (!NativeCopyBundle.same(copied, current.layout()) || !command.request().jidHex().equals(current.jidHex())) {
            throw new IllegalArgumentException("native_copy_mapping_changed");
        }
        NativeBusinessImageExport.validateCatalogue(session.connection(), command.request(), current.layout(), seconds(budget));
    }
    private static NativeLifecycleGuard.Proof guard(NativeCopyObservationExport.Session session, Command command,
        NativeBusinessImageExport.Layout layout, NativeCopyObservationExport.Budget budget) throws Exception {
        var r = command.request();
        var prefix = session.prefix(new NativeJournalPrefixForce.Target(r.journalLibrary(), r.journalName()), seconds(budget));
        budget.seconds();
        return NativeLifecycleGuard.scan(session.connection(), r, layout, command.boundary(), prefix, command.limits()
            .maxRows(), seconds(budget));
    }
    private static byte[] document(Command c, State s, String mode, boolean complete) throws Exception {
        var r = c.request();
        StringBuilder b = new StringBuilder("{\"format_version\":\"quadringent-native-journal-guard-v1\",\"mode\":") .append(q(mode))
            .append(",\"state\":").append(q(complete ? "complete" : "incomplete")) .append(",\"scope_sha256\":").append(q(r.scopeSha256()))
            .append(",\"context_sha256\":").append(q(c.contextSha())) .append(",\"capture_binding_sha256\":").append(q(r.captureBindingSha256()))
            .append(",\"copy_run_id\":").append(q(r.copyRunId())) .append(",\"copy_boundary_sha256\":").append(q(c.boundarySha()))
            .append(",\"bundle_sha256\":").append(q(c.bundleSha())) .append(",\"copy_boundary\":{\"receiver_library\":").append(q(c.boundaryLibrary()))
            .append(",\"receiver_name\":") .append(q(c.boundaryReceiver())).append(",\"sequence\":").append(q(c.boundary()
            .toString())).append('}') .append(",\"business_window\":{\"receiver_library\":").append(q(r.receiverLibrary()))
            .append(",\"receiver_name\":") .append(q(r.receiverName())).append(",\"start_sequence\":").append(q(r.start().toString()))
            .append(",\"end_sequence\":").append(q(r.end().toString())).append('}') .append(",\"copy_layout_sha256\":").append(s.copyLayout == null ? "null" : q(s.copyLayout.sha256()))
            .append(",\"guards\":[");
        if (s.first != null) {
            b.append(withPhase(s.first.json(), mode.equals("after_all_streams") ? "after_all_streams" : "before_business"));
        }
        if (!mode.equals("after_all_streams") && s.last != null) {
            if (s.first != null) {
                b.append(',');
            }
            b.append(withPhase(s.last.json(), "after_business"));
        }
        if (s.failedGuard != null) {
            if (s.first != null || s.last != null) {
                b.append(',');
            }
            b.append(withPhase(s.failedGuard, "failed_guard"));
        }
        b.append("],\"current_observations\":[");
        if (s.before != null) {
            b.append(observationReference(mode.equals("after_all_streams") ? "after_all_streams" : "before_business", s.before));
        }
        if (s.after != null) {
            if (s.before != null) {
                b.append(',');
            }
            b.append(observationReference("after_business", s.after));
        }
        b.append("],\"error_code\":").append(s.failure == null ? "null" : q(s.failure)) .append(",\"native_admission\":\"not_evaluated\",\"transaction_consistency_verified\":false}\n");
        byte[] data = b.toString().getBytes(StandardCharsets.UTF_8);
        if (data.length > MAX_RECEIPT) {
            throw new IllegalArgumentException("guard_receipt_budget");
        }
        return data;
    }
    private static String withPhase(String json, String phase) throws Exception {
        if (!json.startsWith("{")) {
            throw new IllegalArgumentException("guard_receipt_invalid");
        }
        return "{\"phase\":" + q(phase) + "," + json.substring(1);
    }
    private static String observationReference(String phase, NativeCopyObservationExport.Result result) throws Exception {
        byte[] data = result.metadata();
        return "{\"phase\":" + q(phase) + ",\"metadata_bytes\":" + data.length + ",\"metadata_sha256\":" + q(NativeCopyObservationExport.sha(data))
            + ",\"metadata_base64\":" + q(Base64.getEncoder().encodeToString(data)) + ",\"layout_sha256\":" + q(result.layout()
            .sha256()) + "}";
    }
    private static int seconds(NativeCopyObservationExport.Budget budget) throws Exception {
        return Math.min(60, budget.seconds());
    }
    private static BigInteger sequence(String s) {
        if (s == null || !s.matches("0|[1-9][0-9]{0,20}")) {
            throw new IllegalArgumentException("invalid_sequence");
        }
        return new BigInteger(s);
    }
    private static String code(Exception failure) {
        if (failure instanceof NativeLifecycleGuard.GuardFailure guard) {
            return guard.code();
        }
        if (failure instanceof NativeJournalPrefixForce.ForceFailure force) {
            return force.code();
        }
        if (failure instanceof NativeBusinessImageExport.ExportFailure export) {
            return export.code();
        }
        if (failure instanceof NativeCopyObservationExport.ObservationFailure observation) {
            return observation.code();
        }
        if (failure instanceof ReadOnlyMemberIdentity.IdentityFailure member) {
            return member.code();
        }
        if (failure instanceof ReadOnlyFormatIdentity.IdentityFailure format) {
            return format.code();
        }
        if (failure instanceof NativeCopyBundle.BundleFailure) {
            return "copy_bundle_invalid";
        }
        if (failure instanceof IllegalArgumentException) {
            return "invalid_input";
        }
        return "source_unavailable";
    }
    private static final class Receipt implements AutoCloseable {
        private final FileChannel channel;
        private boolean closed;
        Receipt(Path path) throws Exception {
            Set<OpenOption> options = Set.of(StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);
            channel = FileChannel.open(path, options, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")));
        }
        void write(byte[] data, NativeCopyObservationExport.Budget budget) throws Exception {
            if (closed || data.length > MAX_RECEIPT) {
                throw new IllegalArgumentException("receipt_unavailable");
            }
            ByteBuffer buffer = ByteBuffer.wrap(data);
            while (buffer.hasRemaining()) {
                if (budget != null) {
                    budget.seconds();
                }
                if (channel.write(buffer) <= 0) {
                    throw new java.io.IOException();
                }
            }
            channel.force(true);
            close();
            if (budget != null) {
                budget.seconds();
            }
        }
        @Override public void close() throws Exception {
            if (!closed) {
                closed = true;
                channel.close();
            }
        }
    }
}
