import { UserManager, WebStorageStateStore } from "oidc-client-ts";

import { HelloRequest } from "./greeting_pb";
import { GreeterClient } from "./greeting_grpc_web_pb";

const form = document.querySelector<HTMLFormElement>("#greeting-form");
const nameInput = document.querySelector<HTMLInputElement>("#name");
const result = document.querySelector<HTMLElement>("#result");
const signInButton = document.querySelector<HTMLButtonElement>("#sign-in");

if (!form || !nameInput || !result || !signInButton) {
  throw new Error("gRPC demo page is missing its form elements");
}

const formElement = form;
const nameInputElement = nameInput;
const resultElement = result;
const signInElement = signInButton;

type RuntimeConfig = {
  grpc_web_endpoint: string;
  oidc_authority: string;
  oidc_client_id: string;
  oidc_scope: string;
};

async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch("/config.json");
  if (!response.ok) {
    throw new Error(`Failed to load /config.json: ${response.status}`);
  }
  return response.json() as Promise<RuntimeConfig>;
}

function createUserManager(config: RuntimeConfig): UserManager | null {
  if (!config.oidc_authority || !config.oidc_client_id) return null;
  return new UserManager({
    authority: config.oidc_authority,
    client_id: config.oidc_client_id,
    redirect_uri: `${window.location.origin}${window.location.pathname}`,
    response_type: "code",
    scope: config.oidc_scope,
    userStore: new WebStorageStateStore({ store: sessionStorage }),
    automaticSilentRenew: false,
  });
}

function isAuthCallback(): boolean {
  const params = new URLSearchParams(window.location.search);
  return params.has("code") && params.has("state");
}

async function getAccessToken(userManager: UserManager | null): Promise<string | undefined> {
  if (userManager === null) return undefined;
  const user = await userManager.getUser();
  if (user && !user.expired) return user.access_token;
  await userManager.signinRedirect();
  throw new Error("Redirecting to Authentik for authentication");
}

async function greet(name: string, userManager: UserManager | null, client: GreeterClient): Promise<void> {
  const request = new HelloRequest();
  request.setName(name);

  resultElement.textContent = "Calling…";
  try {
    const token = await getAccessToken(userManager);
    client.sayHello(request, token ? { authorization: `Bearer ${token}` } : {}, (error, response) => {
      if (error) {
        resultElement.textContent = `Error: ${error.message}`;
        return;
      }
      resultElement.textContent = response?.getMessage() ?? "gRPC call returned no response";
    });
  } catch (error) {
    resultElement.textContent = error instanceof Error ? error.message : "Authentication failed";
  }
}

async function main(): Promise<void> {
  const config = await loadRuntimeConfig();
  const client = new GreeterClient(config.grpc_web_endpoint);
  const userManager = createUserManager(config);

  signInElement.hidden = userManager === null;
  if (userManager !== null) {
    signInElement.addEventListener("click", () => {
      void userManager.signinRedirect().catch((error: unknown) => {
        resultElement.textContent = error instanceof Error ? error.message : "Authentication failed";
      });
    });

    if (isAuthCallback()) {
      await userManager.signinRedirectCallback();
      window.history.replaceState({}, "", window.location.pathname);
    }

    const user = await userManager.getUser();
    if (!user || user.expired) {
      resultElement.textContent = "Sign in with Authentik to call the service.";
      return;
    }
    signInElement.hidden = true;
  }

  formElement.addEventListener("submit", (event) => {
    event.preventDefault();
    void greet(nameInputElement.value, userManager, client);
  });
  await greet(nameInputElement.value, userManager, client);
}

void main().catch((error: unknown) => {
  resultElement.textContent = error instanceof Error ? error.message : "Failed to initialize the demo";
});
