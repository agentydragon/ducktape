# LiteLLM ChatGPT alias blocks the event loop

## Finding

The 2026-10-22 restart of `litellm-668f867b7b-9xkn8` was a liveness restart triggered by synchronous interactive ChatGPT authentication during request preprocessing. It was not a deployment roll. The configured public `chatgpt/...` aliases point to CLIProxyAPI, but LiteLLM infers its own built-in `chatgpt` provider from the alias before router resolution.

Image: `git.allegedly.works/ducktape-ci/tana-litellm-proxy:devel-20261017084157-2423379`, digest `sha256:4d3b9deadcdc81db380335860e865cea7ae7318f7ca2aee254ea8705c24c140e`; installed LiteLLM `1.100.1`.

## Evidence

- Pod creation: October 19, 19:40:14 UTC. ReplicaSet creation and last reloader config timestamp: October 19, 07:40:02 UTC.
- October 22, 05:34:35: final successful readiness/liveness responses in the previous container log.
- 05:34:40.965: production prints `Sign in with ChatGPT using device code`. The code is omitted here.
- 05:34:50–05:35:10: three liveness timeouts. Event: `Container litellm failed liveness probe, will be restarted`.
- 05:36:40: previous container exits 137, reason `Error`; replacement starts at 05:36:41 in the same pod.

An isolated pod using the deployed image/config reproduced loss of health responses when given the original synthetic Chat request for `chatgpt/ant-messages/gpt-6-luna`. A SIGUSR1 faulthandler dump placed the main event-loop thread in:

```text
proxy/common_request_processing.py:2083 -> utils.function_setup
utils.py:1084 -> get_llm_provider (Gemini thought-signature preprocessing)
get_llm_provider_logic.py:226 -> provider selection from the public model prefix
llms/chatgpt/chat/transformation.py:35 -> Authenticator.get_access_token
llms/chatgpt/authenticator.py:169 -> _poll_for_authorization_code
llms/chatgpt/authenticator.py:240 -> synchronous time.sleep
```

The authenticator's polling deadline is `15 * 60` seconds. Because this call runs synchronously on the event-loop thread, neither unrelated HTTP requests nor liveness/readiness handlers can run. Kubelet's configured three failures at 10-second intervals with 5-second HTTP deadlines explain the restart; request/client timeouts cannot unblock this thread.

A second isolated control changed only the public `chatgpt/` alias prefix to `canary-chatgpt/`, retaining upstream model/provider/credentials and the request payload. Health remained HTTP 200 throughout and after the 50-second probe, no interactive-login prompt appeared, and the main-thread dump showed the event loop running normally. Its generation request hit the client's 50-second deadline: this control proves removal of the synchronous auth stall, not a successful Chat generation.

Both diagnostic pods were deleted. Production replicas remained Ready with their prior restart counts.

## Mitigation and verification

[PR #11500](https://github.com/agentydragon/ducktape/pull/11500) renamed the public prefix to `chatgpt-sub/` across the catalog, generated config, callers, and key allowlists, without compatibility aliases. Both deployed replicas exposed six new routes and zero old routes.

Live generation probes using the admin key inside the pod passed:

| Endpoint        | Model                                  | Result                                                                                               |
| --------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `/v1/responses` | `chatgpt-sub/oai-responses/gpt-6-luna` | HTTP 200, requested marker returned; 317 input / 16 output tokens.                                   |
| `/v1/messages`  | `chatgpt-sub/ant-messages/gpt-6-luna`  | HTTP 200 in 5.77 seconds, exact requested marker returned, `end_turn`; 315 input / 13 output tokens. |

During the Messages probe, all liveness requests returned HTTP 200 in at most 20 ms. Both replicas remained Ready with zero restarts afterward. The regular client Secret was absent at probe time, so these requests do not establish client-key readiness. The earlier Chat Completions control's generation timeout remains undiagnosed.

The prefix avoids the collision for configured traffic; it does not disable LiteLLM's built-in provider. Stale callers sending `chatgpt/...` can still enter interactive authentication. The deployed source applies this preprocessing to `anthropic_messages` as well as Chat Completions; Responses bypasses the branch. Do not restore the old aliases, increase probe deadlines, or supply built-in ChatGPT credentials as a remedy for this defect.

## Artifacts

`/tmp/langfuse-cutover-20261022/` on `wyrm2` contains redaction-sensitive previous-container logs, restart events/state, canary/control specs, stack dumps, and synthetic probe scripts/results. Do not publish raw device-login codes or resolved credentials. The upstream preprocessing defect remains; the deployed mitigation is the prefix rename.
