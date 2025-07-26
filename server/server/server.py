# server.py (OTLP Exporter + HPA/부하분산 기능 통합안)

import time
import grpc
from concurrent import futures
import logging
import os
import sys

# --- OpenTelemetry의 핵심 SDK 및 Exporter ---
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource

# --- gRPC Observability 플러그인 ---
import grpc_observability

# Protobuf 및 헬스 체크 관련 import
import streaming_pb2
import streaming_pb2_grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

# --- 로깅 설정 ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)

# --- OpenTelemetry 설정 ---
otel_collector_endpoint = os.getenv("OTEL_COLLECTOR_ENDPOINT", "localhost:4317")
logging.info(f"Sending metrics to OTEL Collector at: {otel_collector_endpoint}")

resource = Resource(attributes={"service.name": "vac-hub-service"})
exporter = OTLPMetricExporter(endpoint=otel_collector_endpoint, insecure=True)
reader = PeriodicExportingMetricReader(exporter, export_interval_millis=5000)
provider = MeterProvider(metric_readers=[reader], resource=resource)

# --- gRPC 기본 메트릭 플러그인 설정 ---
otel_plugin = grpc_observability.OpenTelemetryPlugin(meter_provider=provider)
otel_plugin.register_global()

# --- 커스텀 메트릭 생성 ---
custom_meter = provider.get_meter("grpc.server.python.streaming_service.custom")

# 1. ★★★ OTLP를 위한 활성 스트림 메트릭 생성 ★★★
# Prometheus의 Gauge와 동일한 역할을 하는 UpDownCounter를 사용합니다.
# 값이 오르거나 내릴 수 있는 카운터입니다.
active_streams_updown_counter = custom_meter.create_up_down_counter(
    name="grpc.server.active_streams",
    unit="1",
    description="The number of currently active gRPC streams."
)

# 기존에 있던 총 메시지 처리량 카운터
processed_message_counter = custom_meter.create_counter(
    name="app.messages.processed.count",
    unit="1",
    description="The total number of messages processed by the streaming service"
)

# StreamerService 클래스
class StreamerService(streaming_pb2_grpc.StreamerServicer):
    def ProcessTextStream(self, request_iterator, context):
        # ★★★ 시작: 요청이 시작되면 UpDownCounter를 1 증가시킵니다 ★★★
        active_streams_updown_counter.add(1)
        logging.info("Stream opened. Active stream count changing.")

        message_count = 0
        try:
            for request in request_iterator:
                message_count += 1
                processed_message_counter.add(1)
                time.sleep(0.01)

            logging.info(f"Stream closed normally. Processed {message_count} messages.")
            return streaming_pb2.TextResponse(message_count=message_count)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.CANCELLED:
                logging.info(f"Stream cancelled by client after {message_count} messages.")
            else:
                logging.error(f"Stream broken by unexpected RpcError: {e}. Processed {message_count} messages.")
            # 오류가 발생해도 message_count를 반환할 수 있도록 수정
            return streaming_pb2.TextResponse(message_count=message_count)
        finally:
            # ★★★ 종료: 요청이 어떻게 끝나든 UpDownCounter를 1 감소시킵니다 ★★★
            active_streams_updown_counter.add(-1)
            logging.info("Stream finished. Active stream count changing.")

def serve():
    # 2. ★★★ gRPC 서버 연결 관리 옵션 추가 ★★★
    server_options = [
        # 테스트를 위해 1분으로 설정 (실제 환경에서는 5~10분으로 조정)
        ('grpc.max_connection_age_ms', 60000), 
        # GOAWAY 신호를 보낸 후, 클라이언트가 진행 중인 요청을 마무리할 수 있도록 
        # 30초의 유예 시간을 줍니다. 이 시간 동안은 연결이 바로 끊기지 않습니다.
        ('grpc.max_connection_age_grace_ms', 30000)
    ]
    
    # 서버 생성 시 options 인자 전달
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=server_options
    )
    
    streaming_pb2_grpc.add_StreamerServicer_to_server(StreamerService(), server)

    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    server.add_insecure_port("[::]:50051")
    server.start()
    logging.info(f"gRPC server started on port 50051 with max_connection_age={server_options[0][1]}ms, grace={server_options[1][1]}ms.")
    server.wait_for_termination()


if __name__ == "__main__":
    try:
        # 3. ★★★ Prometheus 전용 HTTP 서버 시작 코드 제거 ★★★
        # OTLP exporter는 백그라운드에서 주기적으로 메트릭을 전송하므로,
        # 별도의 메트릭용 웹 서버가 필요 없습니다.
        serve()
    finally:
        logging.info("Shutting down...")
        # 전역 플러그인 등록 해제
        otel_plugin.deregister_global()
        # MeterProvider를 종료하여 모든 메트릭이 전송되도록 보장
        provider.shutdown()