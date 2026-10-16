package io.quadringent.as400;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.Reader;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.Charset;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

/** Fixed native business images. Stream completion does not grant native admission. */
public final class NativeBusinessImageExport {
    private static final Pattern NAME = Pattern.compile("[A-Z0-9_$#@]{1,10}");
    private static final Pattern SQL_NAME = Pattern.compile("[A-Za-z_][A-Za-z0-9_]{0,127}");
    private static final Pattern SHA = Pattern.compile("[0-9a-f]{64}");
    private static final Set<String> TECH = Set.of("event_id", "operation", "journal_receiver", "journal_sequence");
    private static final Set<Integer> SBCS = Set.of(37, 273, 367);
    private static final BigInteger MAX_SEQ = BigInteger.TEN.pow(21).subtract(BigInteger.ONE);
    private static final long HARD_BYTES = 16L * 1024 * 1024 * 1024;
    private NativeBusinessImageExport() {
    }
    public record Column(String name, String kind) {
        public Column {
            if (name == null || !SQL_NAME.matcher(name).matches() || TECH.contains(name.toLowerCase(Locale.ROOT)) || !Set.of("int",
                "decimal", "text").contains(kind)) {
                throw new IllegalArgumentException("invalid_column");
            }
        }
    }
    public record Request(String journalLibrary, String journalName, String receiverLibrary, String receiverName,
        String fileLibrary, String fileName, String member, String jidHex, BigInteger start, BigInteger end, String scopeSha256,
        String copyRunId, String captureBindingSha256, String formatName, List<Column> columns) {
        public Request {
            for (String n : new String[] {
                journalLibrary, journalName, receiverLibrary, receiverName, fileLibrary, fileName, member, formatName
            }) {
                name(n);
            }
            if (jidHex == null || !jidHex.matches("[0-9a-fA-F]{20}") || start == null || end == null || start.signum()<1 || end.compareTo(start.subtract(BigInteger.ONE))<0
                || end.compareTo(MAX_SEQ)>0 || start.compareTo(MAX_SEQ)>0 || !hash(scopeSha256) || !hash(captureBindingSha256)
                || copyRunId == null || !copyRunId.matches("[A-Za-z0-9_.-]{1,256}") || columns == null || columns.isEmpty() || columns.size()>512) {
                throw new IllegalArgumentException("invalid_scope");
            }
            columns = List.copyOf(columns);
            Set<String> seen = new HashSet<>();
            for (Column c : columns) {
                if (c == null || !seen.add(c.name().toLowerCase(Locale.ROOT))) {
                    throw new IllegalArgumentException("invalid_columns");
                }
            }
            jidHex = jidHex.toLowerCase(Locale.ROOT);
        }
        String nativeObject() {
            return pad(fileName) + pad(fileLibrary) + pad(member);
        }
        boolean empty() {
            return end.compareTo(start)<0;
        }
    }
    public record Limits(long maxRows, long maxBytes, int maxLineBytes, int maxCellChars, int maxSeconds) {
        public Limits {
            if (maxRows<1 || maxRows>20_000_000 || maxBytes<1 || maxBytes>HARD_BYTES || maxLineBytes<1 || maxLineBytes>8 * 1024 * 1024
                || maxCellChars<1 || maxCellChars>1024 * 1024 || maxSeconds<1 || maxSeconds>900) {
                throw new IllegalArgumentException("invalid_budget");
            }
        }
        public static Limits defaults() {
            return new Limits(2_000_000, 2L * 1024 * 1024 * 1024, 1024 * 1024, 1024 * 1024, 900);
        }
    }
    public record Summary(long rowCount, long byteCount, String sha256, String rowsSha256, String startedAt, String completedAt,
        String nativeAdmission, String layoutSha256, String controllerPolicy) {
    }
    public static final class ExportFailure extends Exception {
        private final String code;
        private ExportFailure(String code) {
            super("native_business_export_error=" + code);
            this.code = code;
        }
        public String code() {
            return code;
        }
    }
    private static ExportFailure fail(String code) {
        return new ExportFailure(code);
    }
    private static void name(String n) {
        if (n == null || !NAME.matcher(n).matches()) {
            throw new IllegalArgumentException("invalid_native_name");
        }
    }
    private static boolean hash(String s) {
        return s != null && SHA.matcher(s).matches();
    }
    private static String pad(String s) {
        return s + " ".repeat(10 - s.length());
    }
    private static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }
    private static String digest(byte[] b) throws Exception {
        return hex(MessageDigest.getInstance("SHA-256").digest(b));
    }
    private static Charset charset(int ccsid) throws ExportFailure {
        if (!SBCS.contains(ccsid)) {
            throw fail("unsupported_layout");
        }
        return Charset.forName(ccsid == 367 ? "US-ASCII" : ccsid == 37 ? "IBM037" : "IBM" + ccsid);
    }
    private static byte[] encode(String value, int ccsid) throws Exception {
        ByteBuffer b = charset(ccsid).newEncoder().onMalformedInput(CodingErrorAction.REPORT) .onUnmappableCharacter(CodingErrorAction.REPORT)
            .encode(CharBuffer.wrap(value));
        byte[] result = new byte[b.remaining()];
        b.get(result);
        return result;
    }
    private static String nativeText(byte[] b, int offset, int length, int ccsid) throws Exception {
        if (offset<0 || length<0 || offset>b.length - length) {
            throw fail("unsupported_layout");
        }
        return charset(ccsid).newDecoder().onMalformedInput(CodingErrorAction.REPORT) .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(b, offset, length)).toString();
    }
    private static String unpad(String s) {
        return s.replaceFirst(" +$", "");
    }
    private static int int4(byte[] b, int p) throws ExportFailure {
        if (p<0 || p>b.length -4) {
            throw fail("unsupported_layout");
        }
        return ByteBuffer.wrap(b, p, 4).getInt();
    }
    private static int uint2(byte[] b, int p) throws ExportFailure {
        if (p<0 || p>b.length -2) {
            throw fail("unsupported_layout");
        }
        return Short.toUnsignedInt(ByteBuffer.wrap(b, p, 2).getShort());
    }
    public static final class NativeDescription {
        private final byte[] file, fileTarget, fileError, format, target, error, rcac, rcacTarget, rcacError;
        private final int fileJobCcsid, formatJobCcsid, rcacJobCcsid;
        public NativeDescription(byte[] file, byte[] fileTarget, byte[] fileError, byte[] format, byte[] target, byte[] error,
            byte[] rcac, byte[] rcacTarget, byte[] rcacError, int fileJobCcsid, int formatJobCcsid, int rcacJobCcsid) {
            this.file = copy(file, 1024 * 1024);
            this.fileTarget = copy(fileTarget, 20);
            this.fileError = copy(fileError, 16);
            this.fileJobCcsid = fileJobCcsid;
            this.format = copy(format, 1024 * 1024);
            this.target = copy(target, 20);
            this.error = copy(error, 16);
            this.rcac = copy(rcac, 1024 * 1024);
            this.rcacTarget = copy(rcacTarget, 20);
            this.rcacError = copy(rcacError, 16);
            this.formatJobCcsid = formatJobCcsid;
            this.rcacJobCcsid = rcacJobCcsid;
        }
        private static byte[] copy(byte[] b, int cap) {
            if (b == null || b.length>cap) {
                throw new IllegalArgumentException("invalid_native_original");
            }
            return b.clone();
        }
        @Override public String toString() {
            return "NativeDescription[originals redacted]";
        }
    }
    public record NativeField(String systemName, int type, int offset, int byteLength, int digits, int scale, int ccsid,
        int allocatedLength) {
    }
    public static final class Layout {
        private final List<NativeField> fields;
        private final int recordLength;
        private final String levelId, sha256, nativeTarget;
        private Layout(List<NativeField> fields, int recordLength, String levelId, String sha, String target) {
            this.fields = List.copyOf(fields);
            this.recordLength = recordLength;
            this.levelId = levelId;
            this.sha256 = sha;
            this.nativeTarget = target;
        }
        public List<NativeField> fields() {
            return fields;
        }
        public int recordLength() {
            return recordLength;
        }
        public String levelId() {
            return levelId;
        }
        public String sha256() {
            return sha256;
        }
        public static Layout parse(NativeDescription original, Request request) throws ExportFailure {
            try {
                if (original == null || request == null) {
                    throw fail("unsupported_layout");
                }
                charset(original.fileJobCcsid);
                charset(original.formatJobCcsid);
                charset(original.rcacJobCcsid);
                checkReply(original.file, original.fileTarget, original.fileError, 49, request, original.fileJobCcsid);
                checkReply(original.format, original.target, original.error, 256, request, original.formatJobCcsid);
                checkReply(original.rcac, original.rcacTarget, original.rcacError, 112, request, original.rcacJobCcsid);
                String rcacStates = nativeText(original.rcac, 21, 2, original.rcacJobCcsid);
                // Qdbfhmnum is the current member count; MAXMBRS is a separate field.
                short memberCount = ByteBuffer.wrap(original.file, 47, 2).getShort();
                if ((original.file[8] & 0x28) != 0 || memberCount != 1
                    || (original.file[23] & 0x01) != 0 || !Set.of("II", "  ").contains(rcacStates) || uint2(original.rcac, 23) != 0
                    || uint2(original.rcac, 25) != 0) {
                    throw fail("unsupported_layout");
                }
                byte[] raw = original.format;
                int length = int4(raw, 66), count = uint2(raw, 143);
                String fmt = unpad(nativeText(raw, 70, 10, original.formatJobCcsid)), level = nativeText(raw, 80, 13, original.formatJobCcsid);
                if (length<1 || length>32766 || !fmt.equals(request.formatName()) || !level.matches("[!-~]{13}") || count<1 || count>512
                    || count != request.columns().size()) {
                    throw fail("unsupported_layout");
                }
                List<NativeField> fields = new ArrayList<>();
                Set<String> seen = new HashSet<>();
                int p = 256, next = 0;
                for (int n = 0;n<count;n++) {
                    int section = int4(raw, p);
                    if (section<112 || section>raw.length - p) {
                        throw fail("unsupported_layout");
                    }
                    String internal = unpad(nativeText(raw, p + 4, 30, original.formatJobCcsid));
                    String external = unpad(nativeText(raw, p + 34, 30, original.formatJobCcsid));
                    if (!internal.equals(external) || !NAME.matcher(internal).matches() || !seen.add(internal)) {
                        throw fail("unsupported_layout");
                    }
                    int type = uint2(raw, p + 64), usage = Byte.toUnsignedInt(raw[p + 66]), offset = int4(raw, p + 67), inputOffset = int4(raw,
                        p + 71);
                    int width = uint2(raw, p + 75), digits = uint2(raw, p + 77), scale = uint2(raw, p + 79), allocated = uint2(raw,
                        p + 91), ccsid = uint2(raw, p + 95);
                    int flags = Byte.toUnsignedInt(raw[p + 85]), mapping = Byte.toUnsignedInt(raw[p + 86]), extended = Byte.toUnsignedInt(raw[p
                        + 111]);
                    if (usage != 3 ||(flags & 0xA8) != 0 || mapping != 0 || extended != 0 || offset != inputOffset || offset != next
                        || width<1 || offset>length - width || allocated != 0 && allocated != width) {
                        throw fail("unsupported_layout");
                    }
                    if (type == 0) {
                        if (!Set.of(2, 4, 8).contains(width) || scale != 0 || digits<1 || digits>(width == 2 ? 5 : width == 4 ? 10 : 19)) {
                            throw fail("unsupported_layout");
                        }
                    } else if (type == 3) {
                        if (digits<1 || digits>63 || scale>digits || width != digits / 2 + 1) {
                            throw fail("unsupported_layout");
                        }
                    } else if (type == 4) {
                        charset(ccsid);
                        if (digits != 0 || scale != 0) {
                            throw fail("unsupported_layout");
                        }
                    } else throw fail("unsupported_layout");
                    fields.add(new NativeField(internal, type, offset, width, digits, scale, ccsid, allocated));
                    next += width;
                    p += section;
                }
                if (next != length || p != raw.length) {
                    throw fail("unsupported_layout");
                }
                MessageDigest h = MessageDigest.getInstance("SHA-256");
                for (byte[] b : new byte[][] {
                    original.file, original.fileTarget, original.fileError, original.format, original.target, original.error,
                        original.rcac, original.rcacTarget, original.rcacError
                }) {
                    h.update(b);
                }
                h.update(ByteBuffer.allocate(12).putInt(original.fileJobCcsid).putInt(original.formatJobCcsid).putInt(original.rcacJobCcsid)
                    .array());
                return new Layout(fields, length, level, hex(h.digest()), pad(request.fileName()) + pad(request.fileLibrary()));
            } catch (ExportFailure e) {
                throw e;
            } catch (Exception e) {
                throw fail("unsupported_layout");
            }
        }
        private static void checkReply(byte[] data, byte[] target, byte[] error, int minimum, Request r, int ccsid) throws Exception {
            if (data.length<minimum || int4(data, 0) != data.length || int4(data, 4) != data.length || target.length != 20
                || error.length != 16 || int4(error, 0) != 16 || int4(error, 4) != 0) {
                throw fail("unsupported_layout");
            }
            for (int i = 8;i<16;i++) {
                if (error[i] != 0) {
                    throw fail("unsupported_layout");
                }
            }
            if (!nativeText(target, 0, 20, ccsid).equals(pad(r.fileName()) + pad(r.fileLibrary()))) {
                throw fail("unsupported_layout");
            }
        }
    }
    public record ControllerOriginal(String reference, String sha256, long byteCount) {
        public ControllerOriginal {
            if (reference == null || reference.isBlank() || reference.length()>4096 || reference.chars().anyMatch(c -> c<32)
                || !hash(sha256) || byteCount<1 || byteCount>HARD_BYTES) {
                throw new IllegalArgumentException("invalid_controller_original");
            }
        }
        @Override public String toString() {
            return "ControllerOriginal[sha256=" + sha256 + ",byteCount=" + byteCount + "]";
        }
    }
    public sealed interface HistoryBinding permits ControllerPermit {
    }
    private static final class ControllerPermit implements HistoryBinding {
        private final Request scope;
        private final String layoutSha, policy;
        private final List<ControllerOriginal> originals;
        private ControllerPermit(Request scope, Layout layout, List<ControllerOriginal> refs, String policy) {
            this.scope = scope;
            this.layoutSha = layout.sha256();
            this.originals = List.copyOf(refs);
            this.policy = policy;
        }
        @Override public String toString() {
            return "ControllerPermit[controller-owned]";
        }
    }
    // Package-private: only the future trusted controller may issue this after validating the referenced originals.
    static HistoryBinding controllerPermit(Request request, Layout layout, ControllerOriginal copyWitness,
        ControllerOriginal historyWindow, ControllerOriginal rcacWindow, ControllerOriginal physicalFile, String policy) throws ExportFailure {
        if (request == null || layout == null || policy == null || !policy.matches("[A-Za-z0-9_.-]{1,128}") || copyWitness == null
            || historyWindow == null || rcacWindow == null || physicalFile == null) {
            throw fail("format_unbound");
        }
        return new ControllerPermit(request, layout, List.of(copyWitness, historyWindow, rcacWindow, physicalFile),
            policy);
    }
    static HistoryBinding guardedPermit(Request request, Layout layout, NativeLifecycleGuard.Proof proof,
        NativeCopyBundle bundle, byte[] currentObservation) throws Exception {
        if (proof == null || !proof.matches(request, layout) || bundle == null || !NativeCopyBundle.same(bundle.validate(request),
            layout)) {
            throw fail("format_unbound");
        }
        byte[] historical = proof.json().getBytes(StandardCharsets.UTF_8);
        ControllerOriginal copy = new ControllerOriginal("validated-copy-bundle", bundle.sha256(), bundle.byteCount());
        ControllerOriginal history = new ControllerOriginal("native-rdf-before-business", digest(historical),
            historical.length);
        ControllerOriginal current = new ControllerOriginal("current-native-observation", digest(currentObservation),
            currentObservation.length);
        return controllerPermit(request, layout, copy, history, current, current, "native-fixed-copy-rdf-v1");
    }
    public static void main(String[] args) {
        int status = NativeBusinessImageProducer.run(args, System.in, System.out, System.err,
            NativeCopyObservationExport.NativeSession::new, false);
        if (status != 0) {
            System.exit(status);
        }
    }
    private static ControllerPermit authorize(Request request, Layout layout, HistoryBinding binding) throws ExportFailure {
        if (!(binding instanceof ControllerPermit p) || request == null || layout == null || !p.scope.equals(request)
            || !p.layoutSha.equals(layout.sha256()) || !layout.nativeTarget.equals(pad(request.fileName()) + pad(request.fileLibrary()))
            || p.originals.size() != 4) {
            throw fail("format_unbound");
        }
        return p;
    }
    @FunctionalInterface public interface OwnedConnection {
        Connection open() throws Exception;
    }
    public static int run(OwnedConnection factory, Request request, Layout layout, HistoryBinding binding, Limits limits,
        OutputStream output, OutputStream errors) {
        try {
            authorize(request, layout, binding);
            try (Connection connection = factory.open()) {
                export(connection, request, layout, binding, limits, output);
            }
            return 0;
        } catch (Exception failure) {
            String code = failure instanceof ExportFailure e ? e.code() : "source_unavailable";
            try {
                errors.write(("native_business_export_error=" + code + "\n").getBytes(StandardCharsets.US_ASCII));
                errors.flush();
            } catch (Exception ignored) {
            }
            return 2;
        }
    }
    private static final class Budget {
        long started = System.nanoTime(), previous = started, bytes, rows;
        final Limits limits;
        final MessageDigest all, rowHash;
        Budget(Limits limits) throws Exception {
            this.limits = limits;
            all = MessageDigest.getInstance("SHA-256");
            rowHash = MessageDigest.getInstance("SHA-256");
        }
        int seconds() throws ExportFailure {
            long now = System.nanoTime(), remaining =(long)limits.maxSeconds() * 1_000_000_000L -(now - started);
            if (now<previous || remaining <= 0) {
                throw fail("deadline");
            }
            previous = now;
            return Math.max(1,(int)Math.min(limits.maxSeconds(), remaining / 1_000_000_000L));
        }
        void emit(OutputStream output, String line, boolean row) throws Exception {
            seconds();
            byte[] b;
            ByteBuffer encoded = StandardCharsets.UTF_8.newEncoder().onMalformedInput(CodingErrorAction.REPORT) .onUnmappableCharacter(CodingErrorAction.REPORT)
                .encode(CharBuffer.wrap(line + "\n"));
            b = new byte[encoded.remaining()];
            encoded.get(b);
            if (b.length>limits.maxLineBytes() || bytes>limits.maxBytes() - b.length) {
                throw fail("byte_budget");
            }
            if (row && rows >= limits.maxRows()) {
                throw fail("row_budget");
            }
            output.write(b);
            if (output instanceof PrintStream p && p.checkError()) {
                throw fail("stream_io");
            }
            all.update(b);
            bytes += b.length;
            if (row) {
                rowHash.update(b);
                rows++;
            }
            seconds();
        }
    }
    private record SqlField(NativeField nativeField, Column column, String sqlType, int jdbcType, int precision,
        int scale) {
    }
    private static String catalogSql() {
        return "SELECT COLUMN_NAME,SYSTEM_COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE,LENGTH,NUMERIC_SCALE," + "IS_NULLABLE,CCSID,HIDDEN,HAS_FLDPROC FROM QSYS2.SYSCOLUMNS WHERE SYSTEM_TABLE_SCHEMA = ? AND "
            + "SYSTEM_TABLE_NAME = ? ORDER BY ORDINAL_POSITION";
    }
    private static final String[] CATALOG = {
        "COLUMN_NAME", "SYSTEM_COLUMN_NAME", "ORDINAL_POSITION", "DATA_TYPE", "LENGTH", "NUMERIC_SCALE", "IS_NULLABLE",
            "CCSID", "HIDDEN", "HAS_FLDPROC"
    };
    static List<Column> columns(Connection connection, String library, String file, int seconds) throws Exception {
        name(library);
        name(file);
        List<Column> result = new ArrayList<>();
        try (PreparedStatement s = connection.prepareStatement(catalogSql())) {
            s.setString(1, library);
            s.setString(2, file);
            s.setMaxRows(513);
            s.setQueryTimeout(seconds);
            try (ResultSet r = s.executeQuery()) {
                metadata(r.getMetaData(), CATALOG, new int[] {
                    Types.VARCHAR, Types.CHAR, Types.INTEGER, Types.VARCHAR, Types.INTEGER, Types.INTEGER, Types.CHAR, Types.INTEGER,
                        Types.CHAR, Types.CHAR
                }, new int[] {
                    128, 10, 10, 8, 10, 10, 1, 10, 1, 1
                }, new int[10]);
                while (r.next()) {
                    if (result.size() == 512) {
                        throw fail("unsupported_layout");
                    }
                    String kind = switch (r.getString(4)) {
                        case "SMALLINT", "INTEGER", "BIGINT" -> "int";
                        case "DECIMAL" -> "decimal";
                        case "CHAR" -> "text";
                        default -> throw fail("unsupported_layout");
                    };
                    result.add(new Column(r.getString(1), kind));
                }
            }
        }
        if (result.isEmpty()) {
            throw fail("unsupported_layout");
        }
        return List.copyOf(result);
    }
    static void validateCatalogue(Connection connection, Request request, Layout layout, int seconds) throws Exception {
        catalogue(connection, request, layout, new Budget(new Limits(512, 1048576, 1048576, 1048576, seconds)));
    }
    private static List<SqlField> catalogue(Connection connection, Request request, Layout layout, Budget budget) throws Exception {
        List<SqlField> result = new ArrayList<>();
        try (PreparedStatement s = connection.prepareStatement(catalogSql())) {
            s.setString(1, request.fileLibrary());
            s.setString(2, request.fileName());
            s.setQueryTimeout(budget.seconds());
            s.setMaxRows(513);
            try (ResultSet r = s.executeQuery()) {
                metadata(r.getMetaData(), CATALOG, new int[] {
                    Types.VARCHAR, Types.CHAR, Types.INTEGER, Types.VARCHAR, Types.INTEGER, Types.INTEGER, Types.CHAR, Types.INTEGER,
                        Types.CHAR, Types.CHAR
                }, new int[] {
                    128, 10, 10, 8, 10, 10, 1, 10, 1, 1
                }, new int[10]);
                while (r.next()) {
                    budget.seconds();
                    int n = result.size();
                    if (n >= layout.fields().size()) {
                        throw fail("catalogue_mismatch");
                    }
                    NativeField f = layout.fields().get(n);
                    Column c = request.columns().get(n);
                    String sqlName = r.getString(1), system = r.getString(2), type = r.getString(4), nullable = r.getString(7),
                        hidden = r.getString(9), proc = r.getString(10);
                    int ordinal = r.getInt(3), length = r.getInt(5);
                    Object scaleObject = r.getObject(6), ccsidObject = r.getObject(8);
                    Integer scale = scaleObject == null ? null :((Number)scaleObject).intValue(), ccsid = ccsidObject == null ? null :((Number)ccsidObject)
                        .intValue();
                    if (!c.name().equals(sqlName) || system == null || !f.systemName().equals(unpad(system)) || ordinal != n + 1 || !"N".equals(nullable)
                        || !"N".equals(hidden) || !"N".equals(proc)) {
                        throw fail("catalogue_mismatch");
                    }
                    String interpret;
                    int jdbc, precision, resultScale = 0;
                    if (f.type() == 0) {
                        String expected = f.byteLength() == 2 ? "SMALLINT" : f.byteLength() == 4 ? "INTEGER" : "BIGINT";
                        if (!expected.equals(type) || !"int".equals(c.kind()) || length != f.byteLength() || scale == null || scale != 0
                            || ccsid != null) {
                            throw fail("catalogue_mismatch");
                        }
                        interpret = expected;
                        jdbc = f.byteLength() == 2 ? Types.SMALLINT : f.byteLength() == 4 ? Types.INTEGER : Types.BIGINT;
                        precision = f.byteLength() == 2 ? 5 : f.byteLength() == 4 ? 10 : 19;
                    } else if (f.type() == 3) {
                        if (!"DECIMAL".equals(type) || !"decimal".equals(c.kind()) || length != f.digits() || scale == null || scale != f.scale()
                            || ccsid != null) {
                            throw fail("catalogue_mismatch");
                        }
                        interpret = "DECIMAL(" + f.digits() + "," + f.scale() + ")";
                        jdbc = Types.DECIMAL;
                        precision = f.digits();
                        resultScale = f.scale();
                    } else {
                        if (!"CHAR".equals(type) || !"text".equals(c.kind()) || length != f.byteLength() || scale != null || ccsid == null
                            || ccsid != f.ccsid()) {
                            throw fail("catalogue_mismatch");
                        }
                        interpret = "CHAR(" + f.byteLength() + ") CCSID " + f.ccsid();
                        jdbc = Types.CHAR;
                        precision = f.byteLength();
                    }
                    result.add(new SqlField(f, c, interpret, jdbc, precision, resultScale));
                }
            }
        }
        if (result.size() != layout.fields().size()) {
            throw fail("catalogue_mismatch");
        }
        return List.copyOf(result);
    }
    private static String from() {
        return " FROM TABLE(QSYS2.DISPLAY_JOURNAL(JOURNAL_LIBRARY => ?, JOURNAL_NAME => ?, " + "STARTING_RECEIVER_LIBRARY => ?, STARTING_RECEIVER_NAME => ?, STARTING_SEQUENCE => ?, "
            + "ENDING_RECEIVER_LIBRARY => ?, ENDING_RECEIVER_NAME => ?, ENDING_SEQUENCE => ?, JOURNAL_CODES " + "=> 'R', EOF_DELAY => 0)) WHERE LEFT(OBJECT,20) = CAST(? AS CHAR(10)) CONCAT CAST(? AS CHAR(10))";
    }
    private static void bind(PreparedStatement s, Request r) throws SQLException {
        s.setString(1, r.journalLibrary());
        s.setString(2, r.journalName());
        s.setString(3, r.receiverLibrary());
        s.setString(4, r.receiverName());
        s.setBigDecimal(5, new BigDecimal(r.start()));
        s.setString(6, r.receiverLibrary());
        s.setString(7, r.receiverName());
        s.setBigDecimal(8, new BigDecimal(r.end()));
        s.setString(9, r.fileName());
        s.setString(10, r.fileLibrary());
    }
    private static String imageSql(List<SqlField> fields, int recordLength) {
        StringBuilder sql = new StringBuilder("SELECT RECEIVER_LIBRARY,RECEIVER_NAME,SEQUENCE_NUMBER,JOURNAL_CODE,JOURNAL_ENTRY_TYPE,OBJECT,"
            + "OBJECT_TYPE,CHAR(HEX(JOURNAL_IDENTIFIER),20) AS JID_HEX,MINIMIZED_ENTRY_DATA," + "MINIMIZED_ON_FIELD_BOUNDARY,INDICATOR_FLAG,NULL_VALUE_INDICATORS,"
            + "BIGINT(OCTET_LENGTH(ENTRY_DATA)) AS PAYLOAD_LENGTH,CASE WHEN OCTET_LENGTH(ENTRY_DATA) = " + recordLength + " THEN CAST(ENTRY_DATA AS BLOB("
            + recordLength + ")) ELSE CAST(NULL AS BLOB(" + recordLength + ")) END AS ENTRY_DATA");
        for (int i = 0;i<fields.size();i++) {
            SqlField f = fields.get(i);
            NativeField n = f.nativeField();
            sql.append(",INTERPRET(CAST(SUBSTR(ENTRY_DATA,").append(n.offset() + 1).append(',').append(n.byteLength()) .append(") AS VARBINARY(")
                .append(n.byteLength()).append(")) AS ").append(f.sqlType()).append(") AS QD_C").append(i);
        }
        return sql + from() + " ORDER BY SEQUENCE_NUMBER";
    }
    private static void metadata(ResultSetMetaData meta, String[] labels, int[] types, int[] precision, int[] scale) throws Exception {
        if (meta == null || meta.getColumnCount() != labels.length) {
            throw fail("metadata_mismatch");
        }
        for (int i = 0;i<labels.length;i++) {
            if (!labels[i].equalsIgnoreCase(meta.getColumnLabel(i + 1)) || meta.getColumnType(i + 1) != types[i] || precision != null
                && meta.getPrecision(i + 1) != precision[i] || scale != null && meta.getScale(i + 1) != scale[i]) {
                throw fail("metadata_mismatch");
            }
        }
    }
    private static void imageMetadata(ResultSetMetaData meta, List<SqlField> fields) throws Exception {
        String[] base = {
            "RECEIVER_LIBRARY", "RECEIVER_NAME", "SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "OBJECT",
                "OBJECT_TYPE", "JID_HEX", "MINIMIZED_ENTRY_DATA", "MINIMIZED_ON_FIELD_BOUNDARY", "INDICATOR_FLAG",
                "NULL_VALUE_INDICATORS", "PAYLOAD_LENGTH", "ENTRY_DATA"
        };
        int[] baseTypes = {
            Types.VARCHAR, Types.VARCHAR, Types.DECIMAL, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.VARCHAR, Types.CHAR,
                Types.CHAR, Types.CHAR, Types.CHAR, Types.VARCHAR, Types.BIGINT, Types.BLOB
        };
        String[] names = Arrays.copyOf(base, base.length + fields.size());
        int[] types = Arrays.copyOf(baseTypes, names.length);
        for (int i = 0;i<fields.size();i++) {
            names[14 + i] = "QD_C" + i;
            types[14 + i] = fields.get(i).jdbcType();
        }
        metadata(meta, names, types, null, null);
        if (meta.getPrecision(3) != 21 || meta.getScale(3) != 0 || meta.getPrecision(8) != 20) {
            throw fail("metadata_mismatch");
        }
        for (int i = 0;i<fields.size();i++) {
            SqlField f = fields.get(i);
            if (meta.getPrecision(15 + i) != f.precision() || meta.getScale(15 + i) != f.scale()) {
                throw fail("metadata_mismatch");
            }
        }
    }
    private static BigInteger sequence(BigDecimal value) throws ExportFailure {
        try {
            BigInteger i = value.toBigIntegerExact();
            if (i.signum()<0 || i.compareTo(MAX_SEQ)>0) {
                throw fail("position_mismatch");
            }
            return i;
        } catch (ExportFailure e) {
            throw e;
        } catch (Exception e) {
            throw fail("position_mismatch");
        }
    }
    private static byte[] image(ResultSet rows, int length, Budget budget) throws Exception {
        if (rows.getLong(13) != length || rows.wasNull()) {
            throw fail("incomplete_image");
        }
        byte[] data = new byte[length];
        try (InputStream input = rows.getBinaryStream(14)) {
            if (input == null) {
                throw fail("incomplete_image");
            }
            int offset = 0;
            while (offset<length) {
                budget.seconds();
                int n = input.read(data, offset, length - offset);
                if (n <= 0) {
                    throw fail("incomplete_image");
                }
                offset += n;
            }
            if (input.read() != -1) {
                throw fail("incomplete_image");
            }
        }
        return data;
    }
    private static String json(String value) throws ExportFailure {
        if (value == null) {
            throw fail("invalid_value");
        }
        StringBuilder out = new StringBuilder("\"");
        for (int i = 0;i<value.length();i++) {
            char c = value.charAt(i);
            if (Character.isHighSurrogate(c)) {
                if (i + 1 == value.length() || !Character.isLowSurrogate(value.charAt(i + 1))) {
                    throw fail("invalid_value");
                }
                out.append(c).append(value.charAt(++i));
            } else if (Character.isLowSurrogate(c)) {
                throw fail("invalid_value");
            } else switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c<32) {
                        out.append(String.format(Locale.ROOT, "\\u%04x",(int)c));
                    } else out.append(c);
                }
            }
        }
        return out.append('"').toString();
    }
    private static String value(ResultSet row, int column, SqlField field, byte[] image, Limits limits, Budget budget) throws Exception {
        NativeField nativeField = field.nativeField();
        if (field.jdbcType() != Types.CHAR) {
            BigDecimal n = row.getBigDecimal(column);
            if (n == null || n.precision()>field.precision() || n.scale() != field.scale()) {
                throw fail("invalid_value");
            }
            if (field.jdbcType() != Types.DECIMAL) {
                BigInteger integer = n.toBigIntegerExact();
                int bits = nativeField.byteLength() * 8;
                if (integer.compareTo(BigInteger.ONE.shiftLeft(bits -1).negate())<0 || integer.compareTo(BigInteger.ONE.shiftLeft(bits -1)
                    .subtract(BigInteger.ONE))>0) {
                    throw fail("invalid_value");
                }
            }
            String text = n.signum() == 0 ? "0" : n.toPlainString();
            if (text.length()>limits.maxCellChars()) {
                throw fail("cell_budget");
            }
            return json(text);
        }
        StringBuilder text = new StringBuilder();
        try (Reader reader = row.getCharacterStream(column)) {
            if (reader == null) {
                throw fail("invalid_value");
            }
            char[] buffer = new char[Math.min(4096, limits.maxCellChars() + 1)];
            while (true) {
                budget.seconds();
                int n = reader.read(buffer, 0, Math.min(buffer.length, limits.maxCellChars() - text.length() + 1));
                if (n<0) {
                    break;
                }
                if (n == 0 || text.length() + n>limits.maxCellChars()) {
                    throw fail("cell_budget");
                }
                text.append(buffer, 0, n);
            }
        }
        byte[] encoded = encode(text.toString(), nativeField.ccsid());
        if (encoded.length != nativeField.byteLength() || !Arrays.equals(image, nativeField.offset(), nativeField.offset()
            + nativeField.byteLength(), encoded, 0, encoded.length)) {
            throw fail("invalid_value");
        }
        return json(text.toString());
    }
    private static String operation(String type) throws ExportFailure {
        if (type == null) {
            throw fail("unsupported_record_entry_type");
        }
        return switch (type) {
            case "PT", "PX" -> "c";
            case "UB" -> "u_before";
            case "UP" -> "u_after";
            case "DL" -> "d";
            default -> throw fail("unsupported_record_entry_type");
        };
    }
    private static void count(Connection connection, Request request, long expected, Budget budget) throws Exception {
        try (PreparedStatement s = connection.prepareStatement("SELECT BIGINT(COUNT(*)) AS ENTRY_COUNT FROM (SELECT SEQUENCE_NUMBER"
            + from() + " FETCH FIRST " +(budget.limits.maxRows() + 1) + " ROWS ONLY) QD_COUNT")) {
            bind(s, request);
            s.setQueryTimeout(budget.seconds());
            try (ResultSet r = s.executeQuery()) {
                metadata(r.getMetaData(), new String[] {
                    "ENTRY_COUNT"
                }, new int[] {
                    Types.BIGINT
                }, null, null);
                if (!r.next()) {
                    throw fail("count_mismatch");
                }
                BigDecimal count = r.getBigDecimal(1);
                if (count == null || !count.equals(BigDecimal.valueOf(expected)) || r.next()) {
                    throw fail("count_mismatch");
                }
            }
        }
    }
    private static String utc() throws ExportFailure {
        Instant now = Instant.now();
        int year = now.atOffset(ZoneOffset.UTC).getYear();
        if (year<1 || year>9999) {
            throw fail("source_clock");
        }
        return now.toString();
    }
    /** Completion is framing plus decode under a controller-supplied mapping, never native admission. */
    @FunctionalInterface interface CompletionGuard {
        void validate() throws Exception;
    }
    public static Summary export(Connection connection, Request request, Layout layout, HistoryBinding binding,
        Limits limits, OutputStream output) throws ExportFailure {
        return export(connection, request, layout, binding, limits, output, null);
    }
    static Summary export(Connection connection, Request request, Layout layout, HistoryBinding binding, Limits limits,
        OutputStream output, CompletionGuard completionGuard) throws ExportFailure {
        ControllerPermit permit = authorize(request, layout, binding);
        try {
            if (connection == null || limits == null || output == null) {
                throw fail("invalid_input");
            }
            Budget budget = new Budget(limits);
            connection.setReadOnly(true);
            budget.seconds();
            List<SqlField> fields = catalogue(connection, request, layout, budget);
            String started = utc();
            long count = 0;
            BigInteger previous = null;
            if (!request.empty()) {
                try (PreparedStatement s = connection.prepareStatement(imageSql(fields, layout.recordLength()), ResultSet.TYPE_FORWARD_ONLY,
                    ResultSet.CONCUR_READ_ONLY)) {
                    bind(s, request);
                    s.setQueryTimeout(budget.seconds());
                    s.setFetchSize(1000);
                    s.setMaxRows((int)limits.maxRows() + 1);
                    try (ResultSet r = s.executeQuery()) {
                        imageMetadata(r.getMetaData(), fields);
                        budget.emit(output, "{\"format_version\":\"quadringent-native-row-stream-v1\",\"kind\":\"header\"," + "\"dataset\":\"journal\",\"scope_sha256\":"
                            + json(request.scopeSha256()) + ",\"started_at\":" + json(started) + "}", false);
                        while (r.next()) {
                            budget.seconds();
                            if (count == limits.maxRows()) {
                                throw fail("row_budget");
                            }
                            if (!request.receiverLibrary().equals(r.getString(1)) || !request.receiverName().equals(r.getString(2))) {
                                throw fail("position_mismatch");
                            }
                            BigInteger seq = sequence(r.getBigDecimal(3));
                            if (seq.compareTo(request.start())<0 || seq.compareTo(request.end())>0 || previous != null && seq.compareTo(previous) <= 0) {
                                throw fail("position_mismatch");
                            }
                            if (!"R".equals(r.getString(4)) || !request.nativeObject().equals(r.getString(6)) || !"*FILE".equals(r.getString(7))
                                || r.getString(8) == null || !request.jidHex().equals(r.getString(8).toLowerCase(Locale.ROOT))) {
                                throw fail("identity_mismatch");
                            }
                            String type = r.getString(5), op = operation(type), nulls = r.getString(12);
                            if (!"0".equals(r.getString(9)) || !"0".equals(r.getString(10)) || nulls == null || !(nulls.matches(" *") || nulls.equals("0".repeat(fields.size())))
                                || Set.of("UB", "DL").contains(type) && !"1".equals(r.getString(11))) {
                                throw fail("incomplete_image");
                            }
                            byte[] bytes = image(r, layout.recordLength(), budget);
                            StringBuilder row = new StringBuilder("{\"kind\":\"row\",\"row\":{");
                            for (int i = 0;i<fields.size();i++) {
                                if (i>0) {
                                    row.append(',');
                                }
                                row.append(json(fields.get(i).column().name())).append(':').append(value(r, 15 + i, fields.get(i), bytes,
                                    limits, budget));
                            }
                            String seqText = seq.toString(), event = digest(("ibmi|" + request.journalName() + "|" + request.receiverName()
                                + "|" + seqText) .getBytes(StandardCharsets.UTF_8));
                            row.append(",\"EVENT_ID\":").append(json(event)).append(",\"OPERATION\":").append(json(op)) .append(",\"JOURNAL_RECEIVER\":")
                                .append(json(request.receiverName())).append(",\"JOURNAL_SEQUENCE\":").append(json(seqText)).append("}}");
                            budget.emit(output, row.toString(), true);
                            count++;
                            previous = seq;
                        }
                    }
                }
            } else budget.emit(output, "{\"format_version\":\"quadringent-native-row-stream-v1\",\"kind\":\"header\"," + "\"dataset\":\"journal\",\"scope_sha256\":"
                + json(request.scopeSha256()) + ",\"started_at\":" + json(started) + "}", false);
            if (!request.empty()) {
                count(connection, request, count, budget);
            }
            if (completionGuard != null) {
                completionGuard.validate();
            }
            String complete = utc(), rowsHash = hex(budget.rowHash.digest());
            if (Instant.parse(complete).isBefore(Instant.parse(started))) {
                throw fail("source_clock");
            }
            budget.emit(output, "{\"kind\":\"end\",\"row_count\":" + count + ",\"rows_sha256\":" + json(rowsHash) + ",\"completed_at\":"
                + json(complete) + "}", false);
            output.flush();
            if (output instanceof PrintStream p && p.checkError()) {
                throw fail("stream_io");
            }
            budget.seconds();
            return new Summary(count, budget.bytes, hex(budget.all.digest()), rowsHash, started, complete, "not_evaluated",
                layout.sha256(), permit.policy);
        } catch (ExportFailure e) {
            throw e;
        } catch (SQLException e) {
            throw fail("source_sql");
        } catch (IOException e) {
            throw fail("stream_io");
        } catch (Exception e) {
            throw fail("invalid_value");
        }
    }
}
