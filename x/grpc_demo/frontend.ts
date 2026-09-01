import * as grpc from "@grpc/grpc-js";
import { HelloRequest } from "./greeting_pb";
import { GreeterClient } from "./greeting_grpc_pb";

const name = process.argv[2] ?? "Bazel";
const address = process.env["GRPC_DEMO_ADDRESS"] ?? "127.0.0.1:50051";
const client = new GreeterClient(address, grpc.credentials.createInsecure());
const request = new HelloRequest();
request.setName(name);

client.sayHello(request, (error, response) => {
  if (error) {
    console.error(error.message);
    process.exitCode = 1;
    return;
  }
  if (!response) {
    console.error("gRPC call returned no response");
    process.exitCode = 1;
    return;
  }
  console.log(response.getMessage());
  client.close();
});
