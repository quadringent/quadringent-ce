import { ControlPlaneV2Error, type ControlPlaneV2Client } from './controlPlaneV2Client.ts';
import { NATIVE_VERIFICATION_COPY as COPY } from '../domain/operator.ts';
import type { NativeVerification } from './nativeVerification.ts';

export async function runNativeVerification(client: ControlPlaneV2Client, pipelineId: string, signal?: AbortSignal): Promise<NativeVerification> {
  if (!(await client.getNativeCapabilities(pipelineId, signal)).available) throw new Error(COPY.stale);
  const accepted = await client.requestNativeVerification(pipelineId, false, signal);
  if (!accepted) throw new Error(COPY.failed);
  const observed = await client.getNativeVerification(pipelineId, accepted.id, signal);
  if (observed.id !== accepted.id || observed.pipelineId !== pipelineId || observed.contextValidity !== 'same_context'
    || observed.lifecycle !== 'finished' || observed.contextSha256 !== accepted.contextSha256
    || observed.contextGeneration !== accepted.contextGeneration || observed.scopeSha256 !== accepted.scopeSha256) throw new Error(COPY.stale);
  return observed;
}

export function nativeVerificationError(error: unknown): string {
  if (error instanceof ControlPlaneV2Error) {
    if (error.status === 401) return COPY.session;
    if (error.status === 403) return COPY.forbidden;
    if (error.status === 409) return COPY.stale;
  }
  return COPY.failed;
}
