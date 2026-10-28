import grpc

from devinfra.x.schema_codegen.protobuf import sample_pb2_grpc


def make_sample_stub(channel: grpc.Channel) -> sample_pb2_grpc.SampleServiceStub:
    return sample_pb2_grpc.SampleServiceStub(channel)
