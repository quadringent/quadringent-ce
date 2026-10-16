package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.List;

/** End-to-end producer seams, all data and native replies synthetic. */
public final class NativeBusinessImageProducerTest {
    static int checks;
    static void check(boolean b, String message) {
        checks++;
        if (!b) {
            throw new AssertionError(message);
        }
    }
    static final class Fake extends NativeCopyObservationExportTest.Fake {
        final NativeLifecycleGuardTest.Fake guard = new NativeLifecycleGuardTest.Fake();
        int prefixes;
        boolean lateDdl;
        @Override public Connection connection() {
            Connection observations = super.connection();
            return(Connection)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                Connection.class
            },(o, m, a) -> {
                if (m.getName().equals("prepareStatement")) {
                    String q =(String)a[0];
                    if (q.contains("INTERPRET(") || q.contains("ENTRY_COUNT") && !q.contains("'D,F,R'")) {
                        return catalog.connection() .prepareStatement(q);
                    }
                    if (q.contains("'D,F,R'") || q.contains("QSYS2.JOURNAL_INFO")) {
                        return guard.connection().prepareStatement(q);
                    }
                }
                return m.invoke(observations, a);
            });
        }
        @Override public NativeJournalPrefixForce.Result prefix(NativeJournalPrefixForce.Target target, int seconds) throws Exception {
            prefixes++;
            if (lateDdl && prefixes == 2) {
                var row = new NativeBusinessImageExportTest.NativeRow(26, "CG", 0, "        ");
                row.code = "D";
                guard.source.rows.add(row);
            }
            return NativeJournalPrefixForce.collect(guard, target, seconds);
        }
    }
    static String[] arguments(Path bundle, String sha, Path receipt) {
        return new String[] {
            "TESTLIB", "JRN", "TESTLIB", "RCV1", "TESTLIB", "ITEMS", "ITEMS", NativeBusinessImageExportTest.JID, "21", "25",
                "a".repeat(64), "12345678-1234-4234-8234-123456789abc", "b".repeat(64), "ITEMSFMT",
                "ID,AMOUNT,LONG_BUSINESS_LABEL", "int,decimal,text", "c".repeat(64), "d".repeat(64), "TESTLIB", "RCV1", "20",
                bundle.toString(), sha, receipt.toString(), "1000", "10485760", "60"
        };
    }
    public static void main(String[] args) throws Exception {
        Path directory = Files.createTempDirectory("native-business-producer-test-");
        Path bundle = directory.resolve("copy.bundle");
        byte[] raw = NativeCopyBundleTest.fixture();
        Files.write(bundle, raw);
        String sha = NativeCopyObservationExport.sha(raw);
        try {
            Fake good = new Fake();
            var output = new ByteArrayOutputStream();
            var errors = new ByteArrayOutputStream();
            Path receipt = directory.resolve("guard.json");
            check(run(good, arguments(bundle, sha, receipt), output, errors, false) == 0, "producer success " + errors.toString(StandardCharsets.UTF_8));
            check(output.toString(StandardCharsets.UTF_8).contains("\"row_count\":5"), "five images and footer");
            check(good.prefixes == 2 && good.closed, "guards before/after business and connection closed");
            String document = Files.readString(receipt);
            check(document.contains("before_business") && document.contains("after_business") && document.contains("\"end_sequence\":\"30\""),
                "E0/E1 receipts");
            check(document.contains("not_evaluated") && !document.contains("SYNTHETIC_CANARY_PASSWORD"),
                "no admission or secrets");
            Fake late = new Fake();
            late.lateDdl = true;
            output.reset();
            errors.reset();
            Path failed = directory.resolve("late.json");
            check(run(late, arguments(bundle, sha, failed), output, errors, false) == 2, "late lifecycle change refused");
            check(!output.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\""), "late guard precedes footer");
            check(Files.readString(failed).contains("\"journal_code\":\"D\"") && Files.readString(failed).contains("incomplete"),
                "failing D original retained");
            Fake reused = new Fake();
            output.reset();
            check(run(reused, arguments(bundle, sha, receipt), output, errors, false) == 2 && !reused.opened,
                "receipt CREATE_NEW before source");
            Fake invalid = new Fake();
            output.reset();
            String[] wrong = arguments(bundle, "0".repeat(64), directory.resolve("bad.json"));
            check(run(invalid, wrong, output, errors, false) == 2 && !invalid.opened, "bad bundle before source");
            Fake finalGuard = new Fake();
            output.reset();
            errors.reset();
            check(run(finalGuard, arguments(bundle, sha, directory.resolve("final.json")), output, errors, true) == 0,
                "reusable final guard");
            check(finalGuard.prefixes == 1 && output.toString(StandardCharsets.UTF_8).contains("after_all_streams"),
                "final guard isolated from journal export");
            check(!output.toString(StandardCharsets.UTF_8).contains("before_business") && !output.toString(StandardCharsets.UTF_8)
                .contains("after_business"), "final receipt has only final phase");
        } finally {
            try (var files = Files.list(directory)) {
                for (Path file : files.toList()) {
                    Files.delete(file);
                }
            }
            Files.delete(directory);
        }
        System.out.println("native_business_producer=PASS checks=" + checks + " native=0");
    }
    static int run(Fake fake, String[] args, ByteArrayOutputStream output, ByteArrayOutputStream errors, boolean guard) {
        return NativeBusinessImageProducer.run(args, new ByteArrayInputStream(NativeCopyObservationExportTest.credentials()),
            output, errors,(host, user, password, seconds) -> {
            fake.opened = true;
            return fake;
        }, guard);
    }
}
