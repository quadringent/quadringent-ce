package io.quadringent.as400;

import com.ibm.as400.access.AS400Bin4;
import com.ibm.as400.access.AS400PackedDecimal;
import com.ibm.as400.access.AS400Text;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.StringReader;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Types;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import static io.quadringent.as400.NativeBusinessImageExport.Column;
import static io.quadringent.as400.NativeBusinessImageExport.ControllerOriginal;
import static io.quadringent.as400.NativeBusinessImageExport.ExportFailure;
import static io.quadringent.as400.NativeBusinessImageExport.HistoryBinding;
import static io.quadringent.as400.NativeBusinessImageExport.Layout;
import static io.quadringent.as400.NativeBusinessImageExport.Limits;
import static io.quadringent.as400.NativeBusinessImageExport.NativeDescription;
import static io.quadringent.as400.NativeBusinessImageExport.Request;
import static io.quadringent.as400.NativeBusinessImageExport.Summary;
import static io.quadringent.as400.NativeBusinessImageExport.controllerPermit;
import static io.quadringent.as400.NativeBusinessImageExport.export;
import static io.quadringent.as400.NativeBusinessImageExport.run;

public final class NativeBusinessImageExportTest {
    static int assertions;
    static final String SHA = "a".repeat(64), JID = "00112233445566778899";
    static final String LONG = "LONG_BUSINESS_LABEL";
    static final BigDecimal NUMBER = new BigDecimal(BigInteger.TEN.pow(38).subtract(BigInteger.ONE), 12);
    static String scopeSha = SHA, bindingSha = SHA;
    static void ok(boolean value, String reason) {
        assertions++;
        if (!value) {
            throw new AssertionError(reason);
        }
    }
    static void expectFailure(String code, Throwing task) throws Exception {
        assertions++;
        try {
            task.run();
            throw new AssertionError("missing failure " + code);
        } catch (ExportFailure e) {
            ok(e.code().equals(code), "expected " + code + " got " + e.code());
        }
    }
    interface Throwing {
        void run() throws Exception;
    }
    static byte[] text(String value, int width, int ccsid) {
        return new AS400Text(width, ccsid).toBytes(value);
    }
    static void i(byte[] b, int p, int n) {
        ByteBuffer.wrap(b, p, 4).putInt(n);
    }
    static void s(byte[] b, int p, int n) {
        ByteBuffer.wrap(b, p, 2).putShort((short)n);
    }
    static void put(byte[] b, int p, String str, int len, int ccsid) {
        System.arraycopy(text(str, len, ccsid), 0, b, p, len);
    }
    static String pad(String s) {
        return s + " ".repeat(10 - s.length());
    }
    static Request request() {
        return new Request("TESTLIB", "JRN", "TESTLIB", "RCV1", "TESTLIB", "ITEMS", "ITEMS", JID, BigInteger.valueOf(21),
            BigInteger.valueOf(25), scopeSha, "12345678-1234-4234-8234-123456789abc", bindingSha, "ITEMSFMT", List.of(new Column("ID",
            "int"), new Column("AMOUNT", "decimal"), new Column(LONG, "text")));
    }
    static byte[] descriptor(int ccsid) {
        byte[] b = new byte[256 + 3 * 112];
        i(b, 0, b.length);
        i(b, 4, b.length);
        i(b, 66, 32);
        put(b, 70, "ITEMSFMT", 10, ccsid);
        put(b, 80, "ABC1234567890", 13, ccsid);
        s(b, 143, 3);
        String[] names = {
            "ID", "AMOUNT", "LABEL"
        };
        int[] types = {
            0, 3, 4
        }, offsets = {
            0, 4, 24
        }, lengths = {
            4, 20, 8
        }, digits = {
            10, 38, 0
        }, scales = {
            0, 12, 0
        };
        for (int n = 0;n<3;n++) {
            int p = 256 + n * 112;
            i(b, p, 112);
            put(b, p + 4, names[n], 30, ccsid);
            put(b, p + 34, names[n], 30, ccsid);
            s(b, p + 64, types[n]);
            b[p + 66] = 3;
            i(b, p + 67, offsets[n]);
            i(b, p + 71, offsets[n]);
            s(b, p + 75, lengths[n]);
            s(b, p + 77, digits[n]);
            s(b, p + 79, scales[n]);
            s(b, p + 95, n == 2 ? 273 : 0);
        }
        return b;
    }
    static NativeDescription description(byte[] data, int ccsid) {
        byte[] error = new byte[16];
        i(error, 0, 16);
        byte[] rcac = new byte[112];
        i(rcac, 0, 112);
        i(rcac, 4, 112);
        put(rcac, 21, "II", 2, ccsid);
        return new NativeDescription(inactiveFile(), text(pad("ITEMS") + pad("TESTLIB"), 20, ccsid), error, data, text(pad("ITEMS")
            + pad("TESTLIB"), 20, ccsid), error, rcac, text(pad("ITEMS") + pad("TESTLIB"), 20, ccsid), error, ccsid, ccsid,
            ccsid);
    }
    static byte[] inactiveFile() {
        byte[] data = new byte[49];
        i(data, 0, 49);
        i(data, 4, 49);
        data[8] = 2;
        s(data, 47, 1);
        return data;
    }
    static byte[] inactiveRcac() {
        byte[] b = new byte[112];
        i(b, 0, 112);
        i(b, 4, 112);
        put(b, 21, "II", 2, 273);
        return b;
    }
    static Layout layout() throws Exception {
        return Layout.parse(description(descriptor(273), 273), request());
    }
    static HistoryBinding permit(Layout l) throws Exception {
        return controllerPermit(request(), l, new ControllerOriginal("copy-witness", SHA, 100), new ControllerOriginal("history-window",
            "b".repeat(64), 100), new ControllerOriginal("rcac-window", "c".repeat(64), 100), new ControllerOriginal("physical-file",
            "d".repeat(64), 100), "offline-fixture-policy-not-native-admitted");
    }
    static byte[] image(int id, String label) {
        byte[] b = new byte[32];
        System.arraycopy(new AS400Bin4().toBytes(id), 0, b, 0, 4);
        System.arraycopy(new AS400PackedDecimal(38, 12).toBytes(NUMBER), 0, b, 4, 20);
        System.arraycopy(text(label, 8, 273), 0, b, 24, 8);
        return b;
    }
    static final class NativeRow {
        String receiver = "RCV1", library = "TESTLIB", object = pad("ITEMS") + pad("TESTLIB") + pad("ITEMS"), jid = JID;
        String type, code = "R", objectType = "*FILE", min = "0", fieldMin = "0", flag = "1", nulls = "";
        BigDecimal sequence;
        byte[] image;
        Long length;
        String typedText;
        NativeRow(int seq, String t, int id, String label) {
            sequence = BigDecimal.valueOf(seq);
            type = t;
            image = image(id, label);
        }
    }
    static final class Fake {
        List<NativeRow> rows = new ArrayList<>();
        List<Object[]> catalogue = new ArrayList<>();
        List<String> sql = new ArrayList<>();
        List<Map<Integer, Object>> binds = new ArrayList<>();
        List<StatementState> statements = new ArrayList<>();
        boolean closed, closeFailure, wrongTypedMetadata, extraCatalogue, shortBlob, extraBlob, wrongText;
        BigDecimal countOverride;
        Fake() {
            catalogue.add(new Object[] {
                "ID", "ID", 1, "INTEGER", 4, 0, "N", null, "N", "N"
            });
            catalogue.add(new Object[] {
                "AMOUNT", "AMOUNT", 2, "DECIMAL", 38, 12, "N", null, "N", "N"
            });
            catalogue.add(new Object[] {
                LONG, "LABEL", 3, "CHAR", 8, null, "N", 273, "N", "N"
            });
            rows.add(new NativeRow(21, "PT", 1, "AÄ      "));
            rows.add(new NativeRow(22, "UB", 1, "AÄ      "));
            rows.add(new NativeRow(23, "UP", 1, "BÄ      "));
            rows.add(new NativeRow(24, "DL", 1, "BÄ      "));
            rows.add(new NativeRow(25, "PX", 2, "END     "));
        }
        Connection connection() {
            return(Connection)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                Connection.class
            },(o, m, a) -> {
                if (m.getName().equals("prepareStatement")) {
                    String q =(String)a[0];
                    sql.add(q);
                    Map<Integer, Object> p = new HashMap<>();
                    binds.add(p);
                    StatementState state = new StatementState(q, p);
                    statements.add(state);
                    return state.proxy();
                }
                if (m.getName().equals("close")) {
                    closed = true;
                    if (closeFailure) {
                        throw new SQLException("password=CANARY_SECRET");
                    }
                    return null;
                }
                if (m.getName().equals("setReadOnly")) {
                    ok(Boolean.TRUE.equals(a[0]), "read only");
                    return null;
                }
                if (m.getName().equals("isClosed")) {
                    return closed;
                }
                return value(m.getReturnType());
            });
        }
        final class StatementState {
            String q;
            Map<Integer, Object> p;
            boolean closed;
            int timeout, maxRows, fetchSize;
            StatementState(String q, Map<Integer, Object> p) {
                this.q = q;
                this.p = p;
            }
            PreparedStatement proxy() {
                return(PreparedStatement)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                    PreparedStatement.class
                },(o, m, a) -> {
                    switch (m.getName()) {
                        case "setString" : case "setBigDecimal" : p.put((Integer)a[0], a[1]);
                        return null;
                        case "setQueryTimeout" : timeout =(Integer)a[0];
                        return null;
                        case "setMaxRows" : maxRows =(Integer)a[0];
                        return null;
                        case "setFetchSize" : fetchSize =(Integer)a[0];
                        return null;
                        case "close" : closed = true;
                        return null;
                        case "executeQuery" : return result(q);
                    }
                    return value(m.getReturnType());
                });
            }
        }
        ResultSet result(String q) throws Exception {
            if (q.contains("QSYS2.SYSCOLUMNS")) {
                var data = new ArrayList<>(catalogue);
                if (extraCatalogue) {
                    data.add(catalogue.getLast());
                }
                return records(new String[] {
                    "COLUMN_NAME", "SYSTEM_COLUMN_NAME", "ORDINAL_POSITION", "DATA_TYPE", "LENGTH", "NUMERIC_SCALE", "IS_NULLABLE",
                        "CCSID", "HIDDEN", "HAS_FLDPROC"
                }, new int[] {
                    Types.VARCHAR, Types.CHAR, Types.INTEGER, Types.VARCHAR, Types.INTEGER, Types.INTEGER, Types.CHAR, Types.INTEGER,
                        Types.CHAR, Types.CHAR
                }, new int[] {
                    128, 10, 10, 8, 10, 10, 1, 10, 1, 1
                }, new int[10], data);
            }
            if (q.contains("ENTRY_COUNT")) {
                return records(new String[] {
                    "ENTRY_COUNT"
                }, new int[] {
                    Types.BIGINT
                }, new int[] {
                    19
                }, new int[] {
                    0
                }, Collections.singletonList(new Object[] {
                    countOverride == null ? BigDecimal.valueOf(rows.size()) : countOverride
                }));
            }
            ok(q.contains("INTERPRET(CAST(SUBSTR(ENTRY_DATA,1,4) AS VARBINARY(4)) AS INTEGER)"), "integer SQL slice");
            ok(q.contains("SUBSTR(ENTRY_DATA,5,20)") && q.contains("AS DECIMAL(38,12)"), "decimal exact slice");
            int ccsid =((Number)catalogue.get(2)[7]).intValue();
            ok(q.contains("SUBSTR(ENTRY_DATA,25,8)") && q.contains("AS CHAR(8) CCSID " + ccsid), "text SQL explicit CCSID");
            ok(q.contains("CAST(ENTRY_DATA AS BLOB(32))") && q.contains("ELSE CAST(NULL AS BLOB(32))"),
                "bounded BLOB projection retains bad-length row");
            ok(!q.contains("OBJECT_NAME =>") && !q.contains("OBJECT_MEMBER =>") && !q.contains("INCOMPLETE_DATA") && !q.contains("JOURNAL_ENTRY_TYPES")
                && !q.contains(" IN ("), "no current-JID API filter/fictional ABI");
            List<Object[]> data = new ArrayList<>();
            for (var r : rows) {
                Object[] a = new Object[17];
                a[0] = r.library;
                a[1] = r.receiver;
                a[2] = r.sequence;
                a[3] = r.code;
                a[4] = r.type;
                a[5] = r.object;
                a[6] = r.objectType;
                a[7] = r.jid;
                a[8] = r.min;
                a[9] = r.fieldMin;
                a[10] = r.flag;
                a[11] = r.nulls;
                a[12] = r.length == null ?(long)r.image.length : r.length;
                a[13] = r.image;
                try {
                    a[14] = new BigDecimal(((Number)new AS400Bin4().toObject(r.image, 0)).toString());
                    a[15] = new AS400PackedDecimal(38, 12).toObject(r.image, 4);
                    a[16] = r.typedText != null ? r.typedText : new AS400Text(8, ccsid).toObject(r.image, 24);
                } catch (Exception e) {
                    throw new SQLException("password=CANARY_SECRET", e);
                }
                if (wrongText) {
                    a[16] = "trimmed";
                }
                data.add(a);
            }
            return records(new String[] {
                "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT",
                    "OBJECT_TYPE", "JID_HEX", "MINIMIZED_ENTRY_DATA", "MINIMIZED_ON_FIELD_BOUNDARY", "INDICATOR_FLAG",
                    "NULL_VALUE_INDICATORS", "PAYLOAD_LENGTH", "ENTRY_DATA", "QD_C0", "QD_C1", "QD_C2"
            }, new int[] {
                Types.VARCHAR, Types.VARCHAR, Types.DECIMAL, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.VARCHAR, Types.CHAR,
                    Types.CHAR, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.BIGINT, Types.BLOB, Types.INTEGER, wrongTypedMetadata ? Types.DOUBLE : Types.DECIMAL,
                    Types.CHAR
            }, new int[] {
                10, 10, 21, 1, 2, 30, 10, 20, 1, 1, 1, 8000, 19, Integer.MAX_VALUE, 10, 38, 8
            }, new int[] {
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12, 0
            }, data);
        }
        ResultSet records(String[] labels, int[] types, int[] precision, int[] scale, List<Object[]> data) {
            int[] pos = {
                -1
            };
            boolean[] wasNull = {
                false
            };
            ResultSetMetaData meta =(ResultSetMetaData)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                ResultSetMetaData.class
            },(o, m, a) -> {
                int col = a != null && a.length>0 && a[0]instanceof Integer ?(Integer)a[0] -1 : 0;
                return switch (m.getName()) {
                    case "getColumnCount" -> labels.length;
                    case "getColumnLabel" -> labels[col];
                    case "getColumnType" -> types[col];
                    case "getPrecision" -> precision[col];
                    case "getScale" -> scale[col];
                    default -> value(m.getReturnType());
                };
            });
            return(ResultSet)Proxy.newProxyInstance(getClass().getClassLoader(), new Class[] {
                ResultSet.class
            },(o, m, a) -> {
                if (m.getName().equals("next")) {
                    return ++pos[0]<data.size();
                }
                if (m.getName().equals("getMetaData")) {
                    return meta;
                }
                if (m.getName().equals("wasNull")) {
                    return wasNull[0];
                }
                if (m.getName().startsWith("get") && a != null && a.length>0) {
                    int col = a[0]instanceof Integer ?(Integer)a[0] -1 : Arrays.asList(labels).indexOf(a[0]);
                    Object v = data.get(pos[0])[col];
                    wasNull[0] = v == null;
                    return switch (m.getName()) {
                        case "getString" -> v == null ? null : v.toString();
                        case "getInt" -> v == null ? 0 :((Number)v).intValue();
                        case "getLong" -> v == null ? 0L :((Number)v).longValue();
                        case "getBigDecimal" -> v == null ? null : new BigDecimal(v.toString());
                        case "getObject" -> v;
                        case "getCharacterStream" -> v == null ? null : new StringReader(v.toString());
                        case "getBinaryStream" -> v == null ? null : new ByteArrayInputStream(shortBlob ? Arrays.copyOf((byte[])v,((byte[])v)
                            .length -1) : extraBlob ? Arrays.copyOf((byte[])v,((byte[])v).length + 1) :(byte[])v);
                        default -> value(m.getReturnType());
                    };
                }
                return value(m.getReturnType());
            });
        }
    }
    static Object value(Class< ? > type) {
        if (type == boolean.class) {
            return false;
        }
        if (type == int.class) {
            return 0;
        }
        if (type == long.class) {
            return 0L;
        }
        return null;
    }
    static byte[] exported(Fake f, HistoryBinding proof) throws Exception {
        var out = new ByteArrayOutputStream();
        Summary summary = export(f.connection(), request(), layout(), proof, Limits.defaults(), out);
        ok(summary.rowCount() == 5, "five images including final PX");
        ok(summary.nativeAdmission().equals("not_evaluated"), "not admitted");
        ok(summary.byteCount() == out.size(), "bytecount");
        ok(summary.sha256().equals(hex(MessageDigest.getInstance("SHA-256").digest(out.toByteArray()))), "full hash");
        ok(f.statements.stream().allMatch(s -> s.closed && s.timeout>0), "query closure/timeouts");
        return out.toByteArray();
    }
    static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }
    static void failed(Fake f, String code) throws Exception {
        var out = new ByteArrayOutputStream();
        expectFailure(code,() -> export(f.connection(), request(), layout(), permit(layout()), Limits.defaults(), out));
        ok(!out.toString(UTF_8).contains("\"kind\":\"end\""), "no complete footer after error");
    }
    static final Charset UTF_8 = StandardCharsets.UTF_8;
    public static void main(String[] args) throws Exception {
        if (args.length>1) {
            scopeSha = args[1];
        }
        Layout l = layout();
        ok(l.fields().size() == 3 && l.recordLength() == 32, "typed descriptor");
        var raw = descriptor(273);
        var desc = description(raw, 273);
        raw[66] = 99;
        ok(Layout.parse(desc, request()).recordLength() == 32, "defensive original copy");
        HistoryBinding p = permit(l);
        Fake good = new Fake();
        byte[] stream = exported(good, p);
        String body = new String(stream, UTF_8);
        ok(body.contains(NUMBER.toPlainString()), "precision38 exact");
        ok(body.contains("AÄ      "), "Unicode/trailing spaces");
        ok(body.contains("\"u_before\"") && body.contains("\"u_after\"") && body.contains("\"d\""), "all operations");
        String[] lines = body.split("\n");
        ok(lines.length == 7, "header five rows footer");
        byte[] rows = String.join("\n", Arrays.copyOfRange(lines, 1, 6)).concat("\n").getBytes(UTF_8);
        ok(lines[6].contains(hex(MessageDigest.getInstance("SHA-256").digest(rows))), "row digest includes last image");
        ok(good.binds.get(1).values().contains(new BigDecimal("25")), "inclusive end bound");
        byte[] asciiDescriptor = descriptor(273);
        s(asciiDescriptor, 256 + 224 + 95, 367);
        Layout asciiLayout = Layout.parse(description(asciiDescriptor, 273), request());
        Fake ascii = new Fake();
        ascii.catalogue.get(2)[7] = 367;
        for (NativeRow row : ascii.rows) {
            System.arraycopy(text("ab\"\\    ", 8, 367), 0, row.image, 24, 8);
        }
        var asciiOut = new ByteArrayOutputStream();
        Summary asciiSummary = export(ascii.connection(), request(), asciiLayout, permit(asciiLayout), Limits.defaults(),
            asciiOut);
        ok(asciiSummary.rowCount() == 5 && asciiOut.toString(UTF_8).contains("ab\\\"\\\\    "),
            "ASCII quotes slash and trailing spaces");
        byte[] levelChanged = descriptor(273);
        put(levelChanged, 80, "ZZZ1234567890", 13, 273);
        Layout otherLayout = Layout.parse(description(levelChanged, 273), request());
        expectFailure("format_unbound",() -> export(new Fake().connection(), request(), otherLayout, p, Limits.defaults(),
            new ByteArrayOutputStream()));
        try {
            l.fields().clear();
            throw new AssertionError("mutable layout fields");
        } catch (UnsupportedOperationException expected) {
            assertions++;
        }
        expectFailure("format_unbound",() -> export(new Fake().connection(), request(), l, null, Limits.defaults(), new ByteArrayOutputStream()));
        var other = new Request("TESTLIB", "JRN", "TESTLIB", "RCV1", "TESTLIB", "ITEMS", "ITEMS", JID, BigInteger.valueOf(21),
            BigInteger.valueOf(26), scopeSha, request().copyRunId(), SHA, "ITEMSFMT", request().columns());
        expectFailure("format_unbound",() -> export(new Fake().connection(), other, l, p, Limits.defaults(), new ByteArrayOutputStream()));
        for (int off : new int[] {
            256 + 85, 256 + 112 + 85
        }) {
            byte[] d = descriptor(273);
            d[off] =(byte)0x80;
            expectFailure("unsupported_layout",() -> Layout.parse(description(d, 273), request()));
        }
        byte[] d = descriptor(273);
        d[256 + 224 + 85] = 8;
        byte[] variable = d;
        expectFailure("unsupported_layout",() -> Layout.parse(description(variable, 273), request()));
        d = descriptor(273);
        s(d, 256 + 224 + 95, 65535);
        byte[] a = d;
        expectFailure("unsupported_layout",() -> Layout.parse(description(a, 273), request()));
        d = descriptor(273);
        i(d, 256 + 112 + 67, 3);
        byte[] bb = d;
        expectFailure("unsupported_layout",() -> Layout.parse(description(bb, 273), request()));
        d = descriptor(273);
        i(d, 256, 96);
        byte[] cc = d;
        expectFailure("unsupported_layout",() -> Layout.parse(description(cc, 273), request()));
        for (String fault : List.of("member", "jid", "receiver", "library", "min", "minUnknown", "fieldMin", "null",
            "flag", "operation", "BR", "DR", "length", "lastImage", "blobTail", "metadata", "text", "count", "precision")) {
            Fake f = new Fake();
            NativeRow r = f.rows.getLast();
            switch (fault) {
                case "member" -> r.object = pad("ITEMS") + pad("TESTLIB") + pad("OTHER");
                case "jid" -> r.jid = "f".repeat(20);
                case "receiver" -> r.receiver = "OTHER";
                case "library" -> r.library = "OTHER";
                case "min" -> r.min = "1";
                case "minUnknown" -> r.min = "N";
                case "fieldMin" -> r.fieldMin = "1";
                case "null" -> r.nulls = "001";
                case "flag" -> {
                    r.type = "DL";
                    r.flag = "0";
                }
                case "operation" -> r.type = "UR";
                case "BR", "DR" -> r.type = fault;
                case "length" -> r.length = 31L;
                case "lastImage" -> f.shortBlob = true;
                case "blobTail" -> f.extraBlob = true;
                case "metadata" -> f.wrongTypedMetadata = true;
                case "text" -> f.wrongText = true;
                case "count" -> f.countOverride = BigDecimal.valueOf(4);
                case "precision" -> f.catalogue.get(1)[4] = 37;
            }
            String code = switch (fault) {
                case "member", "jid" -> "identity_mismatch";
                case "receiver", "library" -> "position_mismatch";
                case "metadata" -> "metadata_mismatch";
                case "precision" -> "catalogue_mismatch";
                case "text" -> "invalid_value";
                case "count" -> "count_mismatch";
                case "operation", "BR", "DR" -> "unsupported_record_entry_type";
                default -> "incomplete_image";
            };
            failed(f, code);
        }
        Fake order = new Fake();
        order.rows.getLast().sequence = BigDecimal.valueOf(24);
        failed(order, "position_mismatch");
        for (String change : List.of("nullable", "hidden", "procedure", "name")) {
            Fake f = new Fake();
            if (change.equals("nullable")) {
                f.catalogue.get(2)[6] = "Y";
            }
            if (change.equals("hidden")) {
                f.catalogue.get(2)[8] = "P";
            }
            if (change.equals("procedure")) {
                f.catalogue.get(2)[9] = "Y";
            }
            if (change.equals("name")) {
                f.catalogue.get(2)[0] = "OTHER_NAME";
            }
            failed(f, "catalogue_mismatch");
        }
        for (int mutation = 0;mutation<6;mutation++) {
            byte[] data = descriptor(273);
            if (mutation == 0) {
                i(data, 4, data.length + 1);
            }
            if (mutation == 1) {
                i(data, 66, 32767);
            }
            if (mutation == 2) {
                i(data, 256, Integer.MAX_VALUE);
            }
            if (mutation == 3) {
                data[256 + 86] = 1;
            }
            if (mutation == 4) {
                data[256 + 111] = 16;
            }
            if (mutation == 5) {
                s(data, 256 + 224 + 95, 0);
            }
            byte[] invalid = data;
            expectFailure("unsupported_layout",() -> Layout.parse(description(invalid, 273), request()));
        }
        byte[] rcac = new byte[112];
        i(rcac, 0, 112);
        i(rcac, 4, 112);
        put(rcac, 21, "AI", 2, 273);
        byte[] target = text(pad("ITEMS") + pad("TESTLIB"), 20, 273), error = new byte[16];
        i(error, 0, 16);
        var activeRcac = new NativeDescription(inactiveFile(), target, error, descriptor(273), target, error, rcac,
            target, error, 273, 273, 273);
        expectFailure("unsupported_layout",() -> Layout.parse(activeRcac, request()));
        var wrongTarget = new NativeDescription(inactiveFile(), target, error, descriptor(273), text(pad("OTHER") + pad("TESTLIB"),
            20, 273), error, inactiveRcac(), target, error, 273, 273, 273);
        expectFailure("unsupported_layout",() -> Layout.parse(wrongTarget, request()));
        byte[] rcac37 = new byte[112];
        i(rcac37, 0, 112);
        i(rcac37, 4, 112);
        put(rcac37, 21, "II", 2, 37);
        var separateJobs = new NativeDescription(inactiveFile(), target, error, descriptor(273), target, error, rcac37,
            text(pad("ITEMS") + pad("TESTLIB"), 20, 37), error, 273, 273, 37);
        ok(Layout.parse(separateJobs, request()).recordLength() == 32, "separate format and RCAC job CCSIDs");
        var binaryJob = new NativeDescription(inactiveFile(), target, error, descriptor(273), target, error,
            inactiveRcac(), target, error, 273, 65535, 273);
        expectFailure("unsupported_layout",() -> Layout.parse(binaryJob, request()));
        byte[] blankRcac = inactiveRcac();
        put(blankRcac, 21, "  ", 2, 273);
        var noControls = new NativeDescription(inactiveFile(), target, error, descriptor(273), target, error, blankRcac,
            target, error, 273, 273, 273);
        ok(Layout.parse(noControls, request()).recordLength() == 32, "undefined RCAC preserves blank states");
        for (String pair : List.of(" I", "I ", "AA", "AI", "IA", "00")) {
            byte[] mixed = inactiveRcac();
            put(mixed, 21, pair, 2, 273);
            var invalidRcac = new NativeDescription(inactiveFile(), target, error, descriptor(273), target, error, mixed,
                target, error, 273, 273, 273);
            expectFailure("unsupported_layout",() -> Layout.parse(invalidRcac, request()));
        }
        for (int mutation = 0;mutation<6;mutation++) {
            byte[] file = inactiveFile(), controls = inactiveRcac(), fileTarget = target.clone();
            int fileCcsid = 273;
            if (mutation == 0) {
                file[23] = 1;
            }
            if (mutation == 1) {
                s(controls, 23, 1);
            }
            if (mutation == 2) {
                s(controls, 25, 1);
            }
            if (mutation == 3) {
                i(file, 4, 25);
            }
            if (mutation == 4) {
                fileTarget[0] = 0;
            }
            if (mutation == 5) {
                fileCcsid = 65535;
            }
            var invalidFile = new NativeDescription(file, fileTarget, error, descriptor(273), target, error, controls,
                target, error, fileCcsid, 273, 273);
            expectFailure("unsupported_layout",() -> Layout.parse(invalidFile, request()));
        }
        for (int count : new int[]{0, 2, -1}) {
            byte[] file = inactiveFile();
            s(file, 41, 1);
            s(file, 47, count);
            var invalidCount = new NativeDescription(file, target, error, descriptor(273), target, error,
                inactiveRcac(), target, error, 273, 273, 273);
            expectFailure("unsupported_layout", () -> Layout.parse(invalidCount, request()));
        }
        for (int flags : new int[]{0x20, 0x08}) {
            byte[] file = inactiveFile();
            file[8] |= (byte)flags;
            var invalidType = new NativeDescription(file, target, error, descriptor(273), target, error,
                inactiveRcac(), target, error, 273, 273, 273);
            expectFailure("unsupported_layout", () -> Layout.parse(invalidType, request()));
        }
        byte[] truncatedFile = Arrays.copyOf(inactiveFile(), 48);
        i(truncatedFile, 0, 48);
        i(truncatedFile, 4, 48);
        var shortFile = new NativeDescription(truncatedFile, target, error, descriptor(273), target, error,
            inactiveRcac(), target, error, 273, 273, 273);
        expectFailure("unsupported_layout", () -> Layout.parse(shortFile, request()));
        Fake ccsid = new Fake();
        ccsid.catalogue.get(2)[7] = 37;
        failed(ccsid, "catalogue_mismatch");
        Fake extra = new Fake();
        extra.extraCatalogue = true;
        failed(extra, "catalogue_mismatch");
        Fake badPacked = new Fake();
        badPacked.rows.getLast().image[4] =(byte)0xFF;
        failed(badPacked, "source_sql");
        var limited = new ByteArrayOutputStream();
        expectFailure("row_budget",() -> export(new Fake().connection(), request(), l, p, new Limits(4, 1000000, 100000,
            100000, 60), limited));
        ok(!limited.toString(UTF_8).contains("\"kind\":\"end\""), "no footer on row budget");
        Fake close = new Fake();
        close.closeFailure = true;
        var output = new ByteArrayOutputStream();
        var errors = new ByteArrayOutputStream();
        int status = run(close::connection, request(), l, p, Limits.defaults(), output, errors);
        ok(status != 0 && close.closed, "nonzero after close failure");
        ok(!errors.toString(UTF_8).contains("CANARY_SECRET"), "safe error");
        Fake absent = new Fake();
        ok(run(absent::connection, request(), l, null, Limits.defaults(), new ByteArrayOutputStream(), errors) != 0,
            "no untrusted permit");
        if (args.length>0) {
            Files.write(Path.of(args[0]), stream);
        }
        System.out.println("ASSERTIONS=" + assertions + " PASS; native=0; policy=offline-fixture-only");
    }
}
