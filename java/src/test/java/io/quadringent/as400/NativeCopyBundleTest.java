package io.quadringent.as400;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;

/** Deterministic bundle interoperability, without native calls. */
public final class NativeCopyBundleTest {
    static int checks;
    static byte[] fixture() throws Exception {
        var output = new ByteArrayOutputStream();
        try (var data = new DataOutputStream(output)) {
            data.writeBytes("QDTNCB2\n");
            for (int n = 0;n<2;n++) {
                data.writeInt(273);
                data.writeInt(273);
                data.writeInt(273);
                for (byte[] raw : new byte[][] {
                    NativeBusinessImageExportTest.inactiveFile(), NativeBusinessImageExportTest.text("ITEMS     TESTLIB   ", 20,
                        273), NativeCopyObservationExportTest.error(), NativeBusinessImageExportTest.descriptor(273),
                        NativeBusinessImageExportTest.text("ITEMS     TESTLIB   ", 20, 273), NativeCopyObservationExportTest.error(),
                        NativeBusinessImageExportTest.inactiveRcac(), NativeBusinessImageExportTest.text("ITEMS     TESTLIB   ", 20,
                        273), NativeCopyObservationExportTest.error()
                }) {
                    data.writeInt(raw.length);
                    data.write(raw);
                }
            }
        }
        return output.toByteArray();
    }
    public static void main(String[] args) throws Exception {
        if (args.length == 3 && args[0].equals("--read")) {
            var bundle = NativeCopyBundle.read(Path.of(args[1]), args[2]);
            bundle.validate(NativeBusinessImageExportTest.request());
            System.out.println(bundle.descriptionJson());
            return;
        }
        byte[] raw = fixture();
        Path file = Files.createTempFile("native-copy-bundle-test-", ".bin");
        try {
            Files.write(file, raw);
            var bundle = NativeCopyBundle.read(file, NativeCopyObservationExport.sha(raw));
            var layout = bundle.validate(NativeBusinessImageExportTest.request());
            check(layout.levelId().equals("ABC1234567890"), "both typed descriptions");
            check(bundle.descriptionJson().contains("592"), "native byte lengths");
            for (byte[] bad : new byte[][] {
                Arrays.copyOf(raw, raw.length -1), Arrays.copyOf(raw, raw.length + 1), new byte[393601]
            }) {
                Files.write(file, bad);
                reject(file, NativeCopyObservationExport.sha(bad));
            }
            Files.write(file, raw);
            reject(file, "0".repeat(64));
            byte[] magic = raw.clone();
            magic[0] = 'X';
            Files.write(file, magic);
            reject(file, NativeCopyObservationExport.sha(magic));
            byte[] legacy = raw.clone();
            legacy[6] = '1';
            Files.write(file, legacy);
            reject(file, NativeCopyObservationExport.sha(legacy));
            byte[] ccsid = raw.clone();
            Arrays.fill(ccsid, 8, 12,(byte)0xff);
            Files.write(file, ccsid);
            reject(file, NativeCopyObservationExport.sha(ccsid));
        } finally {
            Files.deleteIfExists(file);
        }
        System.out.println("native_copy_bundle=PASS checks=" + checks + " native=0");
    }
    static void reject(Path file, String sha) throws Exception {
        try {
            NativeCopyBundle.read(file, sha);
            throw new AssertionError("bad bundle accepted");
        } catch (NativeCopyBundle.BundleFailure expected) {
            checks++;
        }
    }
    static void check(boolean b, String message) {
        checks++;
        if (!b) {
            throw new AssertionError(message);
        }
    }
}
