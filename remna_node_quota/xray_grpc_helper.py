#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import sys
from typing import Any, Dict, Optional, Tuple

import grpc
from google.protobuf import descriptor_pb2
from google.protobuf import descriptor_pool
from google.protobuf import json_format
from google.protobuf import message_factory


def build_pool() -> descriptor_pool.DescriptorPool:
    pool = descriptor_pool.DescriptorPool()

    stats_file = descriptor_pb2.FileDescriptorProto()
    stats_file.name = "app/stats/command/command.proto"
    stats_file.package = "xray.app.stats.command"
    stats_file.syntax = "proto3"

    stat = stats_file.message_type.add(); stat.name = "Stat"
    f = stat.field.add(); f.name = "name"; f.number = 1; f.label = 1; f.type = 9
    f = stat.field.add(); f.name = "value"; f.number = 2; f.label = 1; f.type = 3

    qreq = stats_file.message_type.add(); qreq.name = "QueryStatsRequest"
    f = qreq.field.add(); f.name = "pattern"; f.number = 1; f.label = 1; f.type = 9
    f = qreq.field.add(); f.name = "reset"; f.number = 2; f.label = 1; f.type = 8

    qresp = stats_file.message_type.add(); qresp.name = "QueryStatsResponse"
    f = qresp.field.add(); f.name = "stat"; f.number = 1; f.label = 3; f.type = 11; f.type_name = ".xray.app.stats.command.Stat"

    svc = stats_file.service.add(); svc.name = "StatsService"
    m = svc.method.add(); m.name = "QueryStats"; m.input_type = ".xray.app.stats.command.QueryStatsRequest"; m.output_type = ".xray.app.stats.command.QueryStatsResponse"
    pool.Add(stats_file)

    proxyman_file = descriptor_pb2.FileDescriptorProto()
    proxyman_file.name = "app/proxyman/command/command.proto"
    proxyman_file.package = "xray.app.proxyman.command"
    proxyman_file.syntax = "proto3"

    remove_user = proxyman_file.message_type.add(); remove_user.name = "RemoveUserOperation"
    f = remove_user.field.add(); f.name = "email"; f.number = 1; f.label = 1; f.type = 9

    alter_req = proxyman_file.message_type.add(); alter_req.name = "AlterInboundRequest"
    f = alter_req.field.add(); f.name = "tag"; f.number = 1; f.label = 1; f.type = 9
    f = alter_req.field.add(); f.name = "operation"; f.number = 2; f.label = 1; f.type = 11; f.type_name = ".xray.app.proxyman.command.RemoveUserOperation"

    alter_resp = proxyman_file.message_type.add(); alter_resp.name = "AlterInboundResponse"
    svc = proxyman_file.service.add(); svc.name = "HandlerService"
    m = svc.method.add(); m.name = "AlterInbound"; m.input_type = ".xray.app.proxyman.command.AlterInboundRequest"; m.output_type = ".xray.app.proxyman.command.AlterInboundResponse"
    pool.Add(proxyman_file)
    return pool


def msg_cls(pool: descriptor_pool.DescriptorPool, full_name: str):
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(full_name))


def read_runtime_config() -> Dict[str, Any]:
    pids = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = open(f"/proc/{name}/cmdline", "rb").read()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "ignore")
        if "rw-core" in cmd and "http+unix://" in cmd and "-config" in cmd:
            pids.append((name, cmd))
    if not pids:
        raise RuntimeError("rw-core process with http+unix config was not found")
    cmd = pids[0][1]
    match = re.search(r"-config\s+(http\+unix://[^ ]+)", cmd)
    if not match:
        raise RuntimeError(f"config url not found in rw-core cmdline: {cmd}")
    cfg = match.group(1)
    rest = cfg.replace("http+unix://", "")
    sock_path = rest.split(".sock", 1)[0] + ".sock"
    http_path = "/" + rest.split(".sock/", 1)[1]

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    req = f"GET {http_path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    s.sendall(req.encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    body = data.split(b"\r\n\r\n", 1)[1]
    return json.loads(body.decode("utf-8"))


def pem_lines_to_bytes(lines: Any) -> Optional[bytes]:
    if not lines:
        return None
    if isinstance(lines, list):
        return ("\n".join(lines) + "\n").encode("utf-8")
    if isinstance(lines, str):
        return (lines.strip() + "\n").encode("utf-8")
    return None


def find_api_tls_material(config: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[bytes], Optional[bytes], str]:
    server_name = "internal.remnawave.local"
    root_cert = client_cert = client_key = None
    for inbound in config.get("inbounds", []) or []:
        if inbound.get("tag") != "REMNAWAVE_API_INBOUND":
            continue
        tls = inbound.get("streamSettings", {}).get("tlsSettings", {})
        server_name = tls.get("serverName") or server_name
        for cert in tls.get("certificates", []) or []:
            usage = cert.get("usage")
            cert_bytes = pem_lines_to_bytes(cert.get("certificate"))
            key_bytes = pem_lines_to_bytes(cert.get("key"))
            if usage == "verify" and cert_bytes:
                root_cert = cert_bytes
            if usage != "verify" and cert_bytes and key_bytes:
                client_cert = cert_bytes
                client_key = key_bytes
    return root_cert, client_cert, client_key, server_name


def make_channel(server: str):
    runtime_config = read_runtime_config()
    root_cert, client_cert, client_key, server_name = find_api_tls_material(runtime_config)
    if root_cert:
        credentials = grpc.ssl_channel_credentials(root_certificates=root_cert, private_key=client_key, certificate_chain=client_cert)
        options = (("grpc.ssl_target_name_override", server_name), ("grpc.default_authority", server_name))
        return grpc.secure_channel(server, credentials, options)
    return grpc.insecure_channel(server)


def query_stats(server: str, pattern: str) -> int:
    pool = build_pool()
    QueryStatsRequest = msg_cls(pool, "xray.app.stats.command.QueryStatsRequest")
    QueryStatsResponse = msg_cls(pool, "xray.app.stats.command.QueryStatsResponse")
    channel = make_channel(server)
    stub = channel.unary_unary(
        "/xray.app.stats.command.StatsService/QueryStats",
        request_serializer=QueryStatsRequest.SerializeToString,
        response_deserializer=QueryStatsResponse.FromString,
    )
    req = QueryStatsRequest(); req.pattern = pattern; req.reset = False
    resp = stub(req, timeout=10)
    print(json.dumps(json_format.MessageToDict(resp, preserving_proto_field_name=True), ensure_ascii=False))
    return 0


def remove_user(server: str, inbound_tag: str, user_id: str) -> int:
    pool = build_pool()
    AlterInboundRequest = msg_cls(pool, "xray.app.proxyman.command.AlterInboundRequest")
    AlterInboundResponse = msg_cls(pool, "xray.app.proxyman.command.AlterInboundResponse")
    channel = make_channel(server)
    stub = channel.unary_unary(
        "/xray.app.proxyman.command.HandlerService/AlterInbound",
        request_serializer=AlterInboundRequest.SerializeToString,
        response_deserializer=AlterInboundResponse.FromString,
    )
    req = AlterInboundRequest(); req.tag = inbound_tag; req.operation.email = user_id
    resp = stub(req, timeout=10)
    print(json.dumps(json_format.MessageToDict(resp, preserving_proto_field_name=True), ensure_ascii=False))
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage:", file=sys.stderr)
        print("  xray_grpc_helper.py stats SERVER PATTERN", file=sys.stderr)
        print("  xray_grpc_helper.py rmu SERVER INBOUND_TAG USER_ID", file=sys.stderr)
        return 2
    action = sys.argv[1]
    if action == "stats":
        if len(sys.argv) != 4:
            return 2
        return query_stats(sys.argv[2], sys.argv[3])
    if action == "rmu":
        if len(sys.argv) != 5:
            return 2
        return remove_user(sys.argv[2], sys.argv[3], sys.argv[4])
    print(f"Unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
