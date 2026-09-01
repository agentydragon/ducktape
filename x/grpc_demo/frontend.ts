import { HelloRequest } from "./greeting_pb";
import { GreeterClient } from "./greeting_grpc_web_pb";

const form = document.querySelector<HTMLFormElement>("#greeting-form");
const nameInput = document.querySelector<HTMLInputElement>("#name");
const result = document.querySelector<HTMLElement>("#result");

if (!form || !nameInput || !result) {
  throw new Error("gRPC demo page is missing its form elements");
}

const resultElement = result;
const grpcWebEndpoint = new URLSearchParams(window.location.search).get("endpoint") ?? "http://127.0.0.1:8080";
const client = new GreeterClient(grpcWebEndpoint);

function greet(name: string): void {
  const request = new HelloRequest();
  request.setName(name);

  client.sayHello(request, {}, (error, response) => {
    if (error) {
      resultElement.textContent = `Error: ${error.message}`;
      return;
    }
    resultElement.textContent = response?.getMessage() ?? "gRPC call returned no response";
  });
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  greet(nameInput.value);
});

greet(nameInput.value);
