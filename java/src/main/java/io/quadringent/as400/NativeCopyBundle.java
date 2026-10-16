package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.Arrays;

/** Bounded native originals supplied by the validated copy-evidence builder. */
public final class NativeCopyBundle {
    private static final int MAX_BYTES = 393600;
    private final String sha256;
    private final int byteCount;
    private final Part before;
    private final Part after;
    private NativeCopyBundle(String sha256, Part before, Part after, int byteCount) {
        this.sha256 = sha256;
        this.byteCount = byteCount;
        this.before = before;
        this.after = after;
    }
    private record Part(int fileCcsid, int formatCcsid, int rcacCcsid, byte[][] blobs) {
        NativeBusinessImageExport.NativeDescription description() {
            return new NativeBusinessImageExport.NativeDescription(blobs[0], blobs[1], blobs[2], blobs[3], blobs[4], blobs[5],
                blobs[6], blobs[7], blobs[8], fileCcsid, formatCcsid, rcacCcsid);
        }
    }
    public static final class BundleFailure extends Exception {
        private static final long serialVersionUID = 1L;
        private BundleFailure() {
            super("native_copy_bundle_invalid");
        }
    }
    public static NativeCopyBundle read(Path path, String expectedSha256) throws BundleFailure {
        try {
            if (path == null || expectedSha256 == null || !expectedSha256.matches("[0-9a-f]{64}")) {
                throw new BundleFailure();
            }
            byte[] raw;
            try (InputStream input = Files.newInputStream(path, LinkOption.NOFOLLOW_LINKS)) {
                raw = input.readNBytes(MAX_BYTES + 1);
                if (raw.length > MAX_BYTES || input.read() != -1) {
                    throw new BundleFailure();
                }
            }
            if (!NativeCopyObservationExport.sha(raw).equals(expectedSha256)) {
                throw new BundleFailure();
            }
            try (DataInputStream input = new DataInputStream(new ByteArrayInputStream(raw))) {
                if (!Arrays.equals(input.readNBytes(8), "QDTNCB2\n".getBytes(StandardCharsets.US_ASCII))) {
                    throw new BundleFailure();
                }
                Part before = part(input);
                Part after = part(input);
                if (input.read() != -1) {
                    throw new BundleFailure();
                }
                return new NativeCopyBundle(expectedSha256, before, after, raw.length);
            }
        } catch (BundleFailure failure) {
            throw failure;
        } catch (Exception failure) {
            throw new BundleFailure();
        }
    }
    private static Part part(DataInputStream input) throws Exception {
        int fileCcsid = input.readInt();
        int formatCcsid = input.readInt();
        int rcacCcsid = input.readInt();
        NativeCopyObservationExport.supportedCcsid(fileCcsid);
        NativeCopyObservationExport.supportedCcsid(formatCcsid);
        NativeCopyObservationExport.supportedCcsid(rcacCcsid);
        byte[][] blobs = new byte[9][];
        for (int n = 0; n < blobs.length; n++) {
            int size = input.readInt();
            if (size < 1 || size > 65536) {
                throw new BundleFailure();
            }
            blobs[n] = input.readNBytes(size);
            if (blobs[n].length != size) {
                throw new BundleFailure();
            }
        }
        return new Part(fileCcsid, formatCcsid, rcacCcsid, blobs);
    }
    public String sha256() {
        return sha256;
    }
    public int byteCount() {
        return byteCount;
    }
    public NativeBusinessImageExport.Layout validate(NativeBusinessImageExport.Request request) throws NativeBusinessImageExport.ExportFailure,
        BundleFailure {
        var left = NativeBusinessImageExport.Layout.parse(before.description(), request);
        var right = NativeBusinessImageExport.Layout.parse(after.description(), request);
        if (!same(left, right)) {
            throw new BundleFailure();
        }
        return left;
    }
    static boolean same(NativeBusinessImageExport.Layout left, NativeBusinessImageExport.Layout right) {
        return left.recordLength() == right.recordLength() && left.levelId().equals(right.levelId()) && left.fields() .equals(right.fields());
    }
    public String descriptionJson() throws Exception {
        return "{\"bundle_sha256\":\"" + sha256 + "\",\"before_file_ccsid\":" + before.fileCcsid() + ",\"after_file_ccsid\":"
            + after.fileCcsid() + ",\"before_format_ccsid\":" + before.formatCcsid() + ",\"before_rcac_ccsid\":" + before.rcacCcsid()
            + ",\"after_format_ccsid\":" + after.formatCcsid() + ",\"after_rcac_ccsid\":" + after.rcacCcsid() + ",\"before_format_bytes\":"
            + before.blobs()[3].length + ",\"after_format_bytes\":" + after.blobs()[3].length + ",\"native_admission\":\"not_evaluated\"}";
    }
}
