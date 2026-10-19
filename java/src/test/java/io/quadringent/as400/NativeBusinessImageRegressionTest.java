package io.quadringent.as400;

import java.io.ByteArrayOutputStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Types;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import static io.quadringent.as400.NativeBusinessImageExport.Column;
import static io.quadringent.as400.NativeBusinessImageExport.ControllerOriginal;
import static io.quadringent.as400.NativeBusinessImageExport.ExportFailure;
import static io.quadringent.as400.NativeBusinessImageExport.HistoryBinding;
import static io.quadringent.as400.NativeBusinessImageExport.Layout;
import static io.quadringent.as400.NativeBusinessImageExport.Limits;
import static io.quadringent.as400.NativeBusinessImageExport.Request;
import static io.quadringent.as400.NativeBusinessImageExport.Summary;
import static io.quadringent.as400.NativeBusinessImageExport.controllerPermit;
import static io.quadringent.as400.NativeBusinessImageExport.export;

public final class NativeBusinessImageRegressionTest {
    static int assertions;
    static void check(boolean condition, String reason) {
        assertions++;
        if (!condition) {
            throw new AssertionError(reason);
        }
    }
    static final NativeBusinessImageExportTest.Fake META = new NativeBusinessImageExportTest.Fake();
    static final String SHA = "a".repeat(64);
    static final String[] CATALOG = {
        "COLUMN_NAME", "SYSTEM_COLUMN_NAME", "ORDINAL_POSITION", "DATA_TYPE", "LENGTH", "NUMERIC_SCALE", "IS_NULLABLE",
            "CCSID", "HIDDEN", "HAS_FLDPROC"
    };
    static String pad(String value) {
        return value + " ".repeat(10 - value.length());
    }
    static void number(byte[] b, int p, int n) {
        ByteBuffer.wrap(b, p, 4).putInt(n);
    }
    static void shortNumber(byte[] b, int p, int n) {
        ByteBuffer.wrap(b, p, 2).putShort((short)n);
    }
    static void text(byte[] b, int p, String value, int n) {
        System.arraycopy(NativeBusinessImageExportTest.text(value, n, 273), 0, b, p, n);
    }
    static Request request() {
        return new Request("TESTLIB", "JRN", "TESTLIB", "RCV1", "TESTLIB", "ITEMS", "ITEMS",
            NativeBusinessImageExportTest.JID, BigInteger.valueOf(21), BigInteger.valueOf(21), SHA, "run104", SHA,
            "ITEMSFMT", List.of(new Column("LABEL", "text")));
    }
    static Layout layout(Request r) throws Exception {
        byte[] b = new byte[368];
        number(b, 0, b.length);
        number(b, 4, b.length);
        number(b, 66, 8);
        text(b, 70, "ITEMSFMT", 10);
        text(b, 80, "ABC1234567890", 13);
        shortNumber(b, 143, 1);
        number(b, 256, 112);
        text(b, 260, "LABEL", 30);
        text(b, 290, "LABEL", 30);
        shortNumber(b, 320, 4);
        b[322] = 3;
        shortNumber(b, 331, 8);
        shortNumber(b, 351, 37);
        return Layout.parse(NativeBusinessImageExportTest.description(b, 273), r);
    }
    static HistoryBinding permit(Request r, Layout l) throws Exception {
        var original = new ControllerOriginal("review-synthetic-only", SHA, 100);
        return controllerPermit(r, l, original, original, original, original, "review-synthetic-only-not-admitted");
    }
    static ResultSet records(String[] names, int[] types, int[] precision, int[] scales, Object[] row) {
        return META.records(names, types, precision, scales, Collections.singletonList(row));
    }
    static ResultSet result(String sql, String value) throws Exception {
        if (sql.contains("QSYS2.SYSCOLUMNS")) {
            return records(CATALOG, new int[] {
                Types.VARCHAR, Types.CHAR, Types.INTEGER, Types.VARCHAR, Types.INTEGER, Types.INTEGER, Types.CHAR, Types.INTEGER,
                    Types.VARCHAR, Types.VARCHAR
            }, new int[] {
                128, 10, 10, 8, 10, 10, 1, 10, 1, 1
            }, new int[10], new Object[] {
                "LABEL", "LABEL", 1, "CHAR", 8, null, "N", 37, "N", "N"
            });
        }
        if (sql.contains("ENTRY_COUNT")) {
            return records(new String[] {
                "ENTRY_COUNT"
            }, new int[] {
                Types.BIGINT
            }, new int[] {
                19
            }, new int[] {
                0
            }, new Object[] {
                BigDecimal.ONE
            });
        }
        if (!sql.contains("SUBSTR(ENTRY_DATA,1,8)") || !sql.contains("AS CHAR(8) CCSID 37")) {
            throw new AssertionError("wrong complete CHAR slice");
        }
        return records(new String[] {
            "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT",
                "OBJECT_TYPE", "JID_HEX", "MINIMIZED_ENTRY_DATA", "MINIMIZED_ON_FIELD_BOUNDARY", "INDICATOR_FLAG",
                "NULL_VALUE_INDICATORS", "PAYLOAD_LENGTH", "ENTRY_DATA", "QD_C0"
        }, new int[] {
            Types.VARCHAR, Types.VARCHAR, Types.DECIMAL, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.VARCHAR, Types.CHAR,
                Types.CHAR, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.BIGINT, Types.BLOB, Types.CHAR
        }, new int[] {
            10, 10, 21, 1, 2, 30, 10, 20, 1, 1, 1, 8000, 19, Integer.MAX_VALUE, 8
        }, new int[15], new Object[] {
            "TESTLIB", "RCV1", BigDecimal.valueOf(21), "R", "PT", pad("ITEMS") + pad("TESTLIB") + pad("ITEMS"), "*FILE",
                NativeBusinessImageExportTest.JID, "0", "0", "1", "", 8L, NativeBusinessImageExportTest.text(value, 8, 37),
                value
        });
    }
    static Connection connection(String value) {
        return(Connection)Proxy.newProxyInstance(NativeBusinessImageRegressionTest.class.getClassLoader(), new Class[] {
            Connection.class
        },(o, m, a) -> {
            if (m.getName().equals("prepareStatement")) {
                String sql =(String)a[0];
                return Proxy.newProxyInstance(NativeBusinessImageRegressionTest.class.getClassLoader(), new Class[] {
                    PreparedStatement.class
                },(s, n, b) -> {
                    if (n.getName().equals("executeQuery")) {
                        return result(sql, value);
                    }
                    return NativeBusinessImageExportTest.value(n.getReturnType());
                });
            }
            return NativeBusinessImageExportTest.value(m.getReturnType());
        });
    }
    static boolean textCase(String label, String value) throws Exception {
        Request r = request();
        Layout l = layout(r);
        var out = new ByteArrayOutputStream();
        try {
            Summary summary = export(connection(value), r, l, permit(r, l), Limits.defaults(), out);
            boolean complete = summary.rowCount() == 1 && out.toString(StandardCharsets.UTF_8).contains("\"kind\":\"end\"");
            check(summary.nativeAdmission().equals("not_evaluated"), "never admission");
            return complete;
        } catch (ExportFailure failure) {
            return false;
        }
    }
    static void tailCase() throws Exception {
        byte[] original = NativeBusinessImageExportTest.descriptor(273);
        byte[] appended = Arrays.copyOf(original, original.length + 112);
        System.arraycopy(original, 256, appended, original.length, 112);
        number(appended, 0, appended.length);
        number(appended, 4, appended.length);
        try {
            Layout.parse(NativeBusinessImageExportTest.description(appended, 273), NativeBusinessImageExportTest.request());
            throw new AssertionError("unconsumed descriptor section accepted");
        } catch (ExportFailure failure) {
            check(failure.code().equals("unsupported_layout"), "descriptor tail refused");
        }
    }
    static void catalogueMetadataCase() throws Exception {
        var valid = new NativeBusinessImageExportTest.Fake();
        check(NativeBusinessImageExport.columns(valid.connection(), "TESTLIB", "ITEMS", 60)
            .equals(NativeBusinessImageExportTest.request().columns()), "measured VARCHAR(1) catalogue flags accepted");
        check(valid.nextCalls == 4, "valid catalogue rows consumed");
        for (boolean exporting : new boolean[]{false, true}) {
            for (int column : new int[]{8, 9}) {
                for (int fault = 0;fault<4;fault++) {
                    var invalid = new NativeBusinessImageExportTest.Fake();
                    switch (fault) {
                        case 0 -> invalid.catalogueTypes[column] = Types.CHAR;
                        case 1 -> invalid.catalogueTypes[column] = Types.INTEGER;
                        case 2 -> invalid.cataloguePrecision[column] = 2;
                        case 3 -> invalid.catalogueScale[column] = 1;
                    }
                    var out = new ByteArrayOutputStream();
                    try {
                        if (exporting) {
                            var layout = NativeBusinessImageExportTest.layout();
                            export(invalid.connection(), NativeBusinessImageExportTest.request(), layout,
                                NativeBusinessImageExportTest.permit(layout), Limits.defaults(), out);
                        } else {
                            NativeBusinessImageExport.columns(invalid.connection(), "TESTLIB", "ITEMS", 60);
                        }
                        throw new AssertionError("invalid catalogue flag metadata accepted");
                    } catch (ExportFailure failure) {
                        check(failure.code().equals("metadata_mismatch"), "strict catalogue metadata refusal");
                    }
                    check(invalid.nextCalls == 0, "metadata refused before row iteration");
                    check(out.size() == 0, "metadata refused before emission");
                    check(invalid.statements.size() == 1 && invalid.statements.getFirst().closed,
                        "catalogue statement closed on metadata refusal");
                }
            }
        }
    }
    public static void main(String[] args) throws Exception {
        if (args.length == 1 && args[0].equals("tail")) {
            tailCase();
            return;
        }
        if (args.length == 1 && args[0].equals("pointer")) {
            check(textCase("literal_pointer_business_value", "*POINTER"), "literal business value preserved");
            return;
        }
        catalogueMetadataCase();
        check(textCase("plain_char_control", "ABCDEFGH"), "fixed CHAR control");
        check(textCase("near_marker_control", "*POINTE "), "near marker control");
        check(textCase("literal_pointer_business_value", "*POINTER"), "literal business value preserved");
        tailCase();
        System.out.println("REGRESSION_ASSERTIONS=" + assertions + " PASS; native=0; review104=closed");
    }
}
