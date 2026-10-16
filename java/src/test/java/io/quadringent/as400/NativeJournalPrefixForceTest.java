package io.quadringent.as400;

import com.ibm.as400.access.AS400ZonedDecimal;
import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.List;

/** Pure force-only ABI tests. No native dispatch is performed. */
public final class NativeJournalPrefixForceTest {
    static int checks;
    static void check(boolean b, String message) {
        checks++;
        if (!b) {
            throw new AssertionError(message);
        }
    }
    static byte[] receiver(String sequence) {
        byte[] b = new byte[58];
        ByteBuffer.wrap(b).putInt(58).putInt(58);
        System.arraycopy(new AS400ZonedDecimal(20, 0).toBytes(new BigDecimal(sequence)), 0, b, 8, 20);
        NativeBusinessImageExportTest.put(b, 28, "RCV1", 10, 273);
        NativeBusinessImageExportTest.put(b, 38, "TESTLIB", 10, 273);
        NativeBusinessImageExportTest.put(b, 48, "*SYSBAS", 10, 273);
        return b;
    }
    static byte[] error() {
        return ByteBuffer.allocate(16).putInt(16).array();
    }
    public static void main(String[] args) throws Exception {
        var request = NativeJournalPrefixForce.prepare(new NativeJournalPrefixForce.Target("TESTLIB", "JRN"), 273);
        check(request.parameters().size() == 9, "nine parameters");
        byte[] key = request.parameters().get(1).input();
        check(Arrays.equals(key, new byte[] {
            0, 0, 0, 1, 0, 0, 0, 4, 0, 0, 0, 1,(byte)0xf2
        }), "one key4 force-only value2");
        check(ByteBuffer.wrap(request.parameters().get(3).input()).getInt() == 0, "no logical entry data");
        check(request.parameters().get(5).outputLength() == 58, "SJNE0100 bound");
        for (String n : List.of("452", "9007199254740993", "18446744073709551615", "99999999999999999999")) {
            var prefix = NativeJournalPrefixForce.parse(new NativeJournalPrefixForce.Reply(true, receiver(n), error()), 273);
            check(prefix.sequence().equals(n), "lossless prefix");
            check(prefix.aspDevice().equals("*SYSBAS"), "special ASP preserved");
        }
        for (int off = 8;off<28;off++) {
            byte[] bad = receiver("452");
            bad[off] =(byte)0xff;
            reject(bad, error());
        }
        byte[] bad = receiver("452");
        ByteBuffer.wrap(bad).putInt(4, 59);
        reject(bad, error());
        byte[] err = error();
        ByteBuffer.wrap(err).putInt(4, 1);
        reject(receiver("452"), err);
        bad = receiver("452");
        bad[27] =(byte)0xd2;
        reject(bad, error());
        for (int ccsid : List.of(0, 65535, 1208)) {
            try {
                NativeJournalPrefixForce.prepare(new NativeJournalPrefixForce.Target("TESTLIB", "JRN"), ccsid);
                throw new AssertionError("unsupported job CCSID");
            } catch (Exception expected) {
                checks++;
            }
        }
        System.out.println("native_journal_prefix_force=PASS checks=" + checks + " native=0");
    }
    static void reject(byte[] raw, byte[] error) throws Exception {
        try {
            NativeJournalPrefixForce.parse(new NativeJournalPrefixForce.Reply(true, raw, error), 273);
            throw new AssertionError("bad prefix accepted");
        } catch (NativeJournalPrefixForce.ForceFailure expected) {
            checks++;
        }
    }
}
