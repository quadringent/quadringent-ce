package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import static io.quadringent.as400.NativeCopyObservationExport.JdbcJob;
import static io.quadringent.as400.NativeCopyObservationExport.JobPair;
import static io.quadringent.as400.NativeCopyObservationExport.JobState;
import static io.quadringent.as400.NativeCopyObservationExport.Session;

/** Offline native originals and JDBC doubles; never opens an IBM i session. */
public final class NativeCopyObservationExportTest {
    static int checks;
    static final String SHA = "a".repeat(64);
    static void check(boolean condition, String reason) {
        checks++;
        if (!condition) {
            throw new AssertionError(reason);
        }
    }
    static String[] arguments() {
        return new String[] {
            "before", SHA, "12345678-1234-4234-8234-123456789abc", "b".repeat(64), "c".repeat(64), "TESTLIB", "RCV1", "20",
                "TESTLIB", "JRN", "TESTLIB", "RCV1", "1", "20", "TESTLIB", "ITEMS", "ITEMS", "ITEMSFMT", "524288", "60"
        };
    }
    static byte[] credentials() {
        return "host.invalid\nTESTUSR\nSYNTHETIC_CANARY_PASSWORD\n".getBytes(StandardCharsets.UTF_8);
    }
    static byte[] error() {
        return ByteBuffer.allocate(16).putInt(16).array();
    }
    static void put(byte[] raw, int offset, String value, int width) {
        NativeBusinessImageExportTest.put(raw, offset, value, width, 273);
    }
    static class Fake implements Session {
        final NativeBusinessImageExportTest.Fake catalog = new NativeBusinessImageExportTest.Fake();
        final List<JobPair> calls = new ArrayList<>();
        String fault = "";
        boolean closed, opened;
        @Override public NativeJournalPrefixForce.Result prefix(NativeJournalPrefixForce.Target target, int seconds) throws Exception {
            throw new AssertionError("copy observation must never force a journal");
        }
        @Override public int ccsid() {
            return fault.equals("ccsid") ? 65535 : 273;
        }
        @Override public JdbcJob jdbcJob() {
            return new JdbcJob("QZDASOINIT", "TESTUSR", "123457");
        }
        @Override public List<JobPair> jobs() {
            return List.copyOf(calls);
        }
        void call() {
            var before = new JobState("QZRCSRVS", "TESTUSR", "123456", 273);
            var after = new JobState("QZRCSRVS", "TESTUSR", fault.equals("job") ? "123458" : "123456", 273);
            calls.add(new JobPair(before, after));
        }
        @Override public Connection connection() {
            return(Connection)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                Connection.class
            },(o, m, a) -> {
                if (m.getName().equals("prepareStatement")) {
                    String sql =(String)a[0];
                    return Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                        PreparedStatement.class
                    },(x, n, b) -> {
                        if (n.getName().equals("getMetaData")) {
                            return witness().getMetaData();
                        }
                        if (n.getName().equals("executeQuery")) {
                            if (sql.contains("QSYS2.SYSCOLUMNS")) {
                                return catalog.result(sql);
                            }
                            return witness();
                        }
                        return NativeBusinessImageExportTest.value(n.getReturnType());
                    });
                }
                return NativeBusinessImageExportTest.value(m.getReturnType());
            });
        }
        ResultSet witness() {
            var labels = new String[] {
                "JID_HEX", "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE",
                    "OBJECT", "OBJECT_TYPE"
            };
            var types = new int[] {
                Types.VARCHAR, Types.VARCHAR, Types.VARCHAR, Types.DECIMAL, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.VARCHAR
            };
            var widths = new int[] {
                20, 10, 10, 21, 1, 2, 30, 10
            };
            List<Object[]> rows = fault.equals("empty") ? List.of() : Collections.singletonList(new Object[] {
                NativeBusinessImageExportTest.JID, "TESTLIB", "RCV1", BigDecimal.valueOf(20), "R", "PT",
                    NativeBusinessImageExportTest.pad("ITEMS") + NativeBusinessImageExportTest.pad("TESTLIB") + NativeBusinessImageExportTest.pad("ITEMS"),
                    "*FILE"
            });
            return catalog.records(labels, types, widths, new int[8], rows);
        }
        @Override public ReadOnlyFormatIdentity.NativeReply file(String library, String file, int seconds) {
            call();
            byte[] receiver = Arrays.copyOf(NativeBusinessImageExportTest.inactiveFile(), 65536);
            if (fault.equals("multi-member")) {
                ByteBuffer.wrap(receiver).putShort(41, (short)1).putShort(47, (short)2);
            }
            if (fault.equals("short-file")) {
                ByteBuffer.wrap(receiver).putInt(48).putInt(48);
            }
            if (fault.equals("file-controls")) {
                receiver[23] = 1;
            }
            return new ReadOnlyFormatIdentity.NativeReply(true, receiver, NativeBusinessImageExportTest.text("ITEMS     TESTLIB   ",
                20, 273), error());
        }
        @Override public ReadOnlyFormatIdentity.NativeReply format(ReadOnlyFormatIdentity.ProgramRequest request, int seconds) {
            check(request.parameters().size() == 10, "fixed FILD0200 API");
            call();
            byte[] receiver = Arrays.copyOf(NativeBusinessImageExportTest.descriptor(273), request.receiverLength());
            return new ReadOnlyFormatIdentity.NativeReply(true, receiver, NativeBusinessImageExportTest.text("ITEMS     TESTLIB   ",
                20, 273), error());
        }
        @Override public ReadOnlyFormatIdentity.NativeReply rcac(String library, String file, String format, int seconds) {
            call();
            byte[] receiver = new byte[65536];
            ByteBuffer.wrap(receiver).putInt(112).putInt(112);
            put(receiver, 21, fault.equals("rcac") ? "AI" : "  ", 2);
            return new ReadOnlyFormatIdentity.NativeReply(true, receiver, NativeBusinessImageExportTest.text("ITEMS     TESTLIB   ",
                20, 273), error());
        }
        @Override public ReadOnlyMemberIdentity.NativeReply member(ReadOnlyMemberIdentity.ProgramRequest request, int seconds) {
            call();
            byte[] raw;
            if (request.program().equals("QUSRMBRD")) {
                raw = new byte[4096];
                ByteBuffer.wrap(raw).putInt(422).putInt(422);
                put(raw, 8, "ITEMS", 10);
                put(raw, 18, "TESTLIB", 10);
                put(raw, 28, fault.equals("member") ? "OTHER" : "ITEMS", 10);
                put(raw, 38, "PF", 10);
                put(raw, 58, "1260101000000", 13);
                for (int off : new int[] {
                    134, 135, 136
                }) {
                    put(raw, off, "0", 1);
                }
                put(raw, 186, "", 13);
                ByteBuffer.wrap(raw).putInt(244, 266).putInt(248, 156);
                put(raw, 390, "0", 1);
                put(raw, 391, "0", 1);
            } else {
                raw = new byte[94];
                ByteBuffer.wrap(raw).putInt(94).putInt(94);
                put(raw, 8, "JRN", 10);
                put(raw, 18, "TESTLIB", 10);
                System.arraycopy(request.parameters().get(3), 0, raw, 28, 10);
                put(raw, 38, "ITEMS", 10);
                put(raw, 48, "TESTLIB", 10);
                put(raw, 58, "ITEMS", 10);
                put(raw, 68, "*QDDS", 10);
                put(raw, 78, "", 16);
            }
            return new ReadOnlyMemberIdentity.NativeReply(true, raw, error());
        }
        @Override public void close() throws Exception {
            closed = true;
            if (fault.equals("close")) {
                throw new SQLException("SYNTHETIC_CANARY_PASSWORD");
            }
        }
    }
    static int run(Fake fake, String[] args, byte[] input, OutputStream out, OutputStream errors) {
        return NativeCopyObservationExport.run(args, new ByteArrayInputStream(input), out, errors,(host, user, password,
            seconds) -> {
            fake.opened = true;
            return fake;
        },() -> 0L,() -> Instant.parse("2026-10-16T08:00:00Z"));
    }
    public static void main(String[] args) throws Exception {
        Fake good = new Fake();
        var out = new ByteArrayOutputStream();
        var errors = new ByteArrayOutputStream();
        check(run(good, arguments(), credentials(), out, errors) == 0, "successful observation " + errors.toString(StandardCharsets.UTF_8));
        String json = out.toString(StandardCharsets.UTF_8);
        check(json.contains("\"format_version\":\"quadringent-native-copy-observation-v2\""), "version");
        check(json.contains("\"context_sha256\":\"" + SHA + "\""), "copy context exact");
        check(json.contains("\"admission\":\"not_evaluated\""), "no native admission");
        check(json.contains("\"native_type\":\"0003\"") && json.contains("LONG_BUSINESS_LABEL"),
            "typed all columns and SQL mapping");
        check(json.contains("receiver_base64") && json.contains("error_sha256") && json.contains("job_before") && json.contains("job_after"),
            "native originals and jobs retained");
        check(good.calls.size() == 7 && good.closed, "all calls and close precede output");
        check(json.contains("\"flags_8\":2") && json.contains("\"flags_23\":0") && json.contains("\"access_control_defined\":false"),
            "FILD0100 evidence");
        check(json.contains("\"row_permissions_state\":\" \"") && json.contains("\"column_masks_state\":\" \"") && json.contains("\"basis\":\"no_controls_defined\""),
            "raw RCAC blanks preserved");
        check(!json.contains("SYNTHETIC_CANARY_PASSWORD"), "secret absent");
        for (String fault : List.of("ccsid", "job", "rcac", "file-controls", "multi-member", "short-file", "member", "empty", "close")) {
            Fake bad = new Fake();
            bad.fault = fault;
            out.reset();
            errors.reset();
            check(run(bad, arguments(), credentials(), out, errors) == 2, "refused " + fault);
            check(out.size() == 0, "no complete observation " + fault);
            check(!errors.toString(StandardCharsets.UTF_8).contains("CANARY"), "safe errors " + fault);
        }
        for (int index : new int[] {
            0, 1, 3, 4, 7, 12, 18, 19
        }) {
            String[] bad = arguments();
            bad[index] = "invalid";
            Fake fake = new Fake();
            out.reset();
            errors.reset();
            check(run(fake, bad, credentials(), out, errors) == 2 && !fake.opened && out.size() == 0,
                "invalid arguments before native " + index);
        }
        for (byte[] bad : List.of("host\nUSER\n".getBytes(), "host\nUSER\nfixture\nextra\n".getBytes(), new byte[] {
            (byte)0xff, '\n'
        })) {
            Fake fake = new Fake();
            out.reset();
            check(run(fake, arguments(), bad, out, errors) == 2 && !fake.opened, "bounded credential input");
        }
        if (args.length == 1 && args[0].equals("--emit-fixture")) {
            System.out.write(json.getBytes(StandardCharsets.UTF_8));
        } else System.out.println("native_copy_observation=PASS checks=" + checks + " native=0");
    }
}
