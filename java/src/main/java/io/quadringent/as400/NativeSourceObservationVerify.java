package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.io.InputStream;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import static io.quadringent.as400.NativeCopyObservationExport.q;

/** Offline parsing only: no JDBC, native dispatch or admission authority. */
public final class NativeSourceObservationVerify {
    private NativeSourceObservationVerify() { }

    public static void main(String[] args) {
        try {
            if (args.length != 2 || !args[1].matches("[0-9a-f]{64}") || System.in.read() != -1) {
                throw new IllegalArgumentException();
            }
            byte[] raw;
            try (InputStream file = Files.newInputStream(Path.of(args[0]), LinkOption.NOFOLLOW_LINKS)) {
                raw = file.readNBytes(3 * 1024 * 1024 + 1);
                if (raw.length > 3 * 1024 * 1024 || file.read() != -1) throw new IllegalArgumentException();
            }
            if (!NativeCopyObservationExport.sha(raw).equals(args[1])) throw new IllegalArgumentException();
            System.out.print(verify(raw));
        } catch (Exception failure) {
            System.err.println("native_source_observation_verify_error=invalid_originals");
            System.exit(2);
        }
    }

    private static byte[] blob(DataInputStream input, int maximum) throws Exception {
        int count = input.readInt();
        if (count < 0 || count > maximum) throw new IllegalArgumentException();
        byte[] result = input.readNBytes(count);
        if (result.length != count) throw new IllegalArgumentException();
        return result;
    }
    private static String text(DataInputStream input) throws Exception {
        byte[] raw = blob(input, 512);
        String value = new String(raw, StandardCharsets.US_ASCII);
        if (!Arrays.equals(raw, value.getBytes(StandardCharsets.US_ASCII))) throw new IllegalArgumentException();
        return value;
    }
    static String verify(byte[] raw) throws Exception {
        try (DataInputStream input = new DataInputStream(new ByteArrayInputStream(raw))) {
            if (!Arrays.equals(input.readNBytes(8), "QDTNSO1\n".getBytes(StandardCharsets.US_ASCII))) throw new IllegalArgumentException();
            String jl = text(input), jn = text(input), rl = text(input), rn = text(input);
            String fl = text(input), fn = text(input), member = text(input), jid = text(input);
            BigInteger start = new BigInteger(text(input)), end = new BigInteger(text(input));
            String scope = text(input), run = text(input), binding = text(input), format = text(input);
            int count = input.readInt();
            if (count < 1 || count > 512) throw new IllegalArgumentException();
            List<NativeBusinessImageExport.Column> columns = new ArrayList<>();
            for (int i = 0; i < count; i++) columns.add(new NativeBusinessImageExport.Column(text(input), text(input)));
            var request = new NativeBusinessImageExport.Request(jl, jn, rl, rn, fl, fn, member, jid,
                    start, end, scope, run, binding, format, columns);
            StringBuilder out = new StringBuilder("{\"format_version\":\"quadringent-native-source-offline-v1\",\"scope_sha256\":")
                    .append(q(scope)).append(",\"observations\":[");
            NativeBusinessImageExport.Layout first = null;
            byte[] firstMember = null;
            for (int n = 0; n < 5; n++) {
                int[] ccsids = {input.readInt(), input.readInt(), input.readInt()};
                byte[][] b = new byte[9][];
                for (int i = 0; i < 9; i++) b[i] = blob(input, 65536);
                var layout = NativeBusinessImageExport.Layout.parse(new NativeBusinessImageExport.NativeDescription(
                        b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], ccsids[0], ccsids[1], ccsids[2]), request);
                if (first != null && !NativeCopyBundle.same(first, layout)) throw new IllegalArgumentException();
                first = layout;
                var memberRequest = new ReadOnlyMemberIdentity.Request(jl, jn, rl, rn, fl, fn, member,
                        new BigInteger(text(input)), new BigInteger(text(input)), scope);
                List<ReadOnlyMemberIdentity.NativeOriginal> originals = new ArrayList<>();
                for (int i = 0; i < 4; i++) originals.add(new ReadOnlyMemberIdentity.NativeOriginal(
                        i == 0 || i == 3 ? "QUSRMBRD" : "QJORJIDI", i == 0 || i == 3 ? "MBRD0200" : "RJID0100",
                        blob(input, 4096), blob(input, 16)));
                byte[] observedMember = ReadOnlyMemberIdentity.validateArchived(memberRequest, ccsids[0], jid, originals);
                if (firstMember != null) ReadOnlyMemberIdentity.sameObservedMember(firstMember, observedMember);
                firstMember = observedMember;
                if (n != 0) out.append(',');
                out.append("{\"level_id\":").append(q(layout.levelId())).append(",\"record_length\":")
                        .append(layout.recordLength()).append(",\"layout_sha256\":").append(q(layout.sha256())).append(",\"fields\":[");
                for (int i = 0; i < layout.fields().size(); i++) {
                    var f = layout.fields().get(i);
                    if (i != 0) out.append(',');
                    out.append("{\"system_name\":").append(q(f.systemName())).append(",\"native_type\":")
                        .append(q(String.format(java.util.Locale.ROOT, "%04X", f.type()))).append(",\"input_offset\":")
                        .append(f.offset()).append(",\"output_offset\":").append(f.offset()).append(",\"byte_length\":")
                        .append(f.byteLength()).append(",\"digits\":").append(f.digits()).append(",\"scale\":")
                        .append(f.scale()).append(",\"ccsid\":").append(f.ccsid()).append(",\"allocated_length\":")
                        .append(f.allocatedLength()).append('}');
                }
                out.append("]}");
            }
            out.append("],\"prefixes\":[");
            for (int n = 0; n < 3; n++) {
                int ccsid = input.readInt();
                var prefix = NativeJournalPrefixForce.parse(new NativeJournalPrefixForce.Reply(true,
                        blob(input, 58), blob(input, 16)), ccsid);
                var parameters = NativeJournalPrefixForce.prepare(new NativeJournalPrefixForce.Target(jl, jn), ccsid).parameters();
                if (n != 0) out.append(',');
                out.append("{\"receiver_library\":").append(q(prefix.receiverLibrary())).append(",\"receiver_name\":")
                        .append(q(prefix.receiverName())).append(",\"sequence\":").append(q(prefix.sequence()))
                        .append(",\"asp_device\":").append(q(prefix.aspDevice())).append(",\"request_parameters\":[");
                for (int i = 0; i < parameters.size(); i++) {
                    var p = parameters.get(i);
                    if (i != 0) out.append(',');
                    out.append("{\"mode\":").append(q(p.mode().name())).append(",\"output_length\":")
                        .append(p.outputLength()).append(",\"input_base64\":")
                        .append(q(java.util.Base64.getEncoder().encodeToString(p.input()))).append(",\"input_sha256\":")
                        .append(q(NativeCopyObservationExport.sha(p.input()))).append('}');
                }
                out.append("]}");
            }
            if (input.read() != -1) throw new IllegalArgumentException();
            return out.append("],\"native_admission\":\"not_evaluated\"}\n").toString();
        }
    }
}
