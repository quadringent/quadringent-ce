package io.quadringent.as400;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.ExtendedIllegalStateException;
import com.ibm.as400.access.ProgramCall;
import com.ibm.as400.access.ProgramParameter;
import com.ibm.as400.access.SecureAS400;
import com.ibm.as400.access.SocketProperties;
import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.List;
import static io.quadringent.as400.NativeCopyObservationExport.JobState;

/** Exercises JTOpen's post-connect property freeze without opening a socket. */
public final class NativeCopyObservationSessionTimeoutTest {
    static int checks;
    static final JobState JOB = new JobState("QZRCSRVS", "TESTUSR", "123456", 273);
    static final JobState OTHER_JOB = new JobState("QZRCSRVS", "TESTUSR", "123457", 273);
    static void check(boolean value, String reason) {
        checks++;
        if (!value) {
            throw new AssertionError(reason);
        }
    }
    static SecureAS400 connected() throws Exception {
        SecureAS400 system = new SecureAS400();
        system.setGuiAvailable(false);
        SocketProperties sockets = new SocketProperties();
        sockets.setSoTimeout(60000);
        sockets.setLoginTimeout(60000);
        system.setSocketProperties(sockets);
        Field frozen = AS400.class.getDeclaredField("propertiesFrozen_");
        frozen.setAccessible(true);
        frozen.setBoolean(system, true);
        return system;
    }
    static final class Call extends ProgramCall {
        final List<Integer> timeouts = new ArrayList<>();
        int runs;
        String program;
        Call(SecureAS400 system) {
            super(system);
        }
        @Override public void setTimeOut(int seconds) {
            super.setTimeOut(seconds);
            timeouts.add(seconds);
        }
        @Override public boolean run() {
            runs++;
            return true;
        }
        @Override public void setProgram(String program, ProgramParameter[] parameters) {
            this.program = program;
        }
        @Override public String getProgram() {
            return program;
        }
    }
    static NativeCopyObservationExport.NativeSession session(SecureAS400 system, Call call,
        NativeCopyObservationExport.JobReader reader) {
        return new NativeCopyObservationExport.NativeSession(system, call, null, JOB,
            new NativeCopyObservationExport.JdbcJob("QZDASOINIT", "TESTUSR", "123458"), reader);
    }
    static void fails(NativeCopyObservationExport.NativeSession session, String code) throws Exception {
        try {
            session.dispatch("QDBRTVFD", new ProgramParameter[0], 12);
            throw new AssertionError("dispatch accepted " + code);
        } catch (NativeCopyObservationExport.ObservationFailure expected) {
            check(expected.code().equals(code), "same refusal code " + code);
        }
    }
    public static void main(String[] args) throws Exception {
        System.setProperty("java.awt.headless", "true");
        SecureAS400 system = connected();
        SocketProperties changed = new SocketProperties();
        changed.setSoTimeout(45000);
        changed.setLoginTimeout(45000);
        try {
            system.setSocketProperties(changed);
            throw new AssertionError("fixture did not freeze connected properties");
        } catch (ExtendedIllegalStateException expected) {
            check(expected.getReturnCode() == ExtendedIllegalStateException.PROPERTY_NOT_CHANGED,
                "real JTOpen connected-state refusal");
        }
        Call call = new Call(system);
        var session = session(system, call, () -> JOB);
        ProgramParameter[] parameters = {new ProgramParameter(4)};
        check(session.dispatch("QDBRTVFD", parameters, 45), "first connected dispatch");
        check(session.dispatch("QDBRTVFD", parameters, 12), "second connected dispatch");
        check(call.timeouts.equals(List.of(45, 12)) && call.runs == 2, "per-call remaining timeout decreases");
        check(system.getSocketProperties().getSoTimeout() == 60000
            && system.getSocketProperties().getLoginTimeout() == 60000, "initial socket limits remain unchanged");
        check(session.jobs().equals(List.of(new NativeCopyObservationExport.JobPair(JOB, JOB),
            new NativeCopyObservationExport.JobPair(JOB, JOB))), "before/after job identities retained");
        check(call.getProgram().equals("/QSYS.LIB/QDBRTVFD.PGM"), "native program unchanged");

        Call beforeCall = new Call(connected());
        var before = session((SecureAS400)beforeCall.getSystem(), beforeCall, () -> OTHER_JOB);
        fails(before, "native_job_changed");
        check(beforeCall.runs == 0 && before.jobs().isEmpty(), "changed job refuses before native call");
        Call afterCall = new Call(connected());
        int[] reads = {0};
        var after = session((SecureAS400)afterCall.getSystem(), afterCall, () -> reads[0]++ == 0 ? JOB : OTHER_JOB);
        fails(after, "native_job_changed");
        check(afterCall.runs == 1 && after.jobs().size() == 1, "changed job after native call remains unqualified");

        Call tlsCall = new Call(connected());
        var tls = session((SecureAS400)tlsCall.getSystem(), tlsCall, () -> JOB);
        String key = "com.ibm.as400.access.SSLTrustAll", prior = System.getProperty(key);
        try {
            System.setProperty(key, "true");
            fails(tls, "tls_required");
            check(tlsCall.runs == 0 && tlsCall.timeouts.isEmpty(), "TLS guard precedes native dispatch");
        } finally {
            if (prior == null) {
                System.clearProperty(key);
            } else {
                System.setProperty(key, prior);
            }
        }
        System.out.println("native_copy_observation_session_timeouts=PASS checks=" + checks + " native=0");
    }
}
