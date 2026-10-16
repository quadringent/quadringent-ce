package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Types;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Actual guard algorithm with synthetic SQL and fixed native-prefix replies. */
public final class NativeLifecycleGuardTest {
    static int checks;
    static void check(boolean b, String reason) {
        checks++;
        if (!b) {
            throw new AssertionError(reason);
        }
    }
    static final class Fake implements NativeJournalPrefixForce.Transport {
        final NativeBusinessImageExportTest.Fake source = new NativeBusinessImageExportTest.Fake();
        final List<String> sql = new ArrayList<>();
        String fault = "";
        int dispatches;
        @Override public NativeCopyObservationExport.JdbcJob sqlJob() {
            return new NativeCopyObservationExport.JdbcJob("QZDASOINIT", "TESTUSR", "123457");
        }
        @Override public NativeCopyObservationExport.JobState job(int seconds) {
            return new NativeCopyObservationExport.JobState("QZRCSRVS", "TESTUSR", fault.equals("job") && dispatches>0 ? "123458" : "123456",
                273);
        }
        @Override public NativeJournalPrefixForce.Reply force(NativeJournalPrefixForce.Request request, int seconds) {
            dispatches++;
            byte[] r = NativeJournalPrefixForceTest.receiver("30");
            if (fault.equals("rotation")) {
                NativeBusinessImageExportTest.put(r, 28, "RCV2", 10, 273);
            }
            return new NativeJournalPrefixForce.Reply(true, r, NativeJournalPrefixForceTest.error());
        }
        @Override public Connection connection() {
            return(Connection)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                Connection.class
            },(o, m, a) -> {
                if (m.getName().equals("prepareStatement")) {
                    String query =(String)a[0];
                    sql.add(query);
                    return Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                        PreparedStatement.class
                    },(x, n, b) -> {
                        if (n.getName().equals("executeQuery")) {
                            return result(query);
                        }
                        return NativeBusinessImageExportTest.value(n.getReturnType());
                    });
                }
                return NativeBusinessImageExportTest.value(m.getReturnType());
            });
        }
        java.sql.ResultSet result(String query) {
            if (query.contains("QSYS2.JOURNAL_INFO")) {
                return source.records(new String[] {
                    "JOURNAL_LIBRARY", "JOURNAL_NAME", "JOURNAL_TYPE", "JOURNAL_STATE"
                }, new int[] {
                    Types.VARCHAR, Types.VARCHAR, Types.VARCHAR, Types.VARCHAR
                }, new int[] {
                    10, 10, 10, 10
                }, new int[4], Collections.singletonList(new Object[] {
                    "TESTLIB", "JRN", "*LOCAL", fault.equals("inactive") ? "*INACTIVE" : "*ACTIVE"
                }));
            }
            if (query.contains("ENTRY_COUNT")) {
                return source.records(new String[] {
                    "ENTRY_COUNT"
                }, new int[] {
                    Types.BIGINT
                }, new int[] {
                    19
                }, new int[1], Collections.singletonList(new Object[] {
                    BigDecimal.valueOf(fault.equals("count") ? 4 : source.rows.size())
                }));
            }
            check(query.contains("JOURNAL_CODES => 'D,F,R'") && !query.contains("OBJECT_NAME =>") && !query.contains("OBJECT_MEMBER =>"),
                "no current-JID API filter");
            check(query.contains("JOURNAL_CODE IN ('D','F') OR (JOURNAL_CODE = 'R' AND LEFT(OBJECT,20)"),
                "D/F population unfiltered by object");
            check(!query.contains("JOURNAL_ENTRY_TYPE IN"), "no entry type hiding");
            var data = new ArrayList<Object[]>();
            for (var r : source.rows) {
                data.add(new Object[] {
                    r.library, r.receiver, r.sequence, r.code, r.type, r.object, r.objectType, r.jid, r.min, r.fieldMin, r.flag, r.nulls,
                        r.image == null ? null :(long)r.image.length, r.image
                });
            }
            return source.records(new String[] {
                "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT",
                    "OBJECT_TYPE", "JID_HEX", "MINIMIZED_ENTRY_DATA", "MINIMIZED_ON_FIELD_BOUNDARY", "INDICATOR_FLAG",
                    "NULL_VALUE_INDICATORS", "PAYLOAD_LENGTH", "ENTRY_DATA"
            }, new int[] {
                Types.VARCHAR, Types.VARCHAR, Types.DECIMAL, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.VARCHAR, Types.CHAR,
                    Types.CHAR, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.BIGINT, Types.BLOB
            }, new int[] {
                10, 10, 21, 1, 2, 30, 10, 20, 1, 1, 1, 8000, 19, 65536
            }, new int[14], data);
        }
    }
    static NativeLifecycleGuard.Proof proof(Fake fake) throws Exception {
        var prefix = NativeJournalPrefixForce.collect(fake, new NativeJournalPrefixForce.Target("TESTLIB", "JRN"), 60);
        return NativeLifecycleGuard.scan(fake.connection(), NativeBusinessImageExportTest.request(),
            NativeBusinessImageExportTest.layout(), BigInteger.valueOf(20), prefix, 1000, 60);
    }
    public static void main(String[] args) throws Exception {
        Fake good = new Fake();
        var proof = proof(good);
        check(proof.json().contains("\"end_sequence\":\"30\""), "native E beyond business C");
        check(proof.json().contains("receiver_base64") && proof.json().contains("entry_data_base64"),
            "native and SQL originals retained");
        check(proof.json().contains("\"state\":\"complete\"") && good.dispatches == 1,
            "one actual fixed force per guard");
        for (String type : List.of("OP", "CL")) {
            Fake harmless = new Fake();
            var entry = new NativeBusinessImageExportTest.NativeRow(26, type, 0, "        ");
            entry.code = "F";
            entry.object = "OTHER     OTHER     OTHER     ";
            entry.image = new byte[0];
            harmless.source.rows.add(entry);
            var observed = proof(harmless);
            check(observed.json().contains("\"journal_entry_type\":\"" + type + "\""), "open/close retained");
        }
        for (String fault : List.of("job", "rotation", "inactive", "count", "member", "jid", "BR", "DR", "UR", "D", "F",
            "short")) {
            Fake bad = new Fake();
            bad.fault = fault;
            var row = bad.source.rows.getLast();
            switch (fault) {
                case "member" -> row.object = "ITEMS     TESTLIB   OTHER     ";
                case "jid" -> row.jid = "f".repeat(20);
                case "BR", "DR", "UR" -> row.type = fault;
                case "D", "F" -> {
                    row.code = fault;
                    row.type = "ZZ";
                    row.object = "OTHER     OTHER     OTHER     ";
                }
                case "short" -> row.image = new byte[31];
                default -> {
                }
            }
            try {
                proof(bad);
                throw new AssertionError("bad guard accepted " + fault);
            } catch (NativeJournalPrefixForce.ForceFailure | NativeLifecycleGuard.GuardFailure expected) {
                check(true, "refused " + fault);
            }
        }
        Fake missingBody = new Fake();
        var unknown = missingBody.source.rows.getLast();
        unknown.code = "F";
        unknown.type = "ZZ";
        unknown.image = null;
        try {
            proof(missingBody);
            throw new AssertionError("unknown F accepted");
        } catch (NativeLifecycleGuard.GuardFailure expected) {
            check(expected.partialJson().contains("\"journal_code\":\"F\""),
                "unknown D/F header retained even with NULL payload");
        }
        System.out.println("native_lifecycle_guard=PASS checks=" + checks + " native=0");
    }
}
