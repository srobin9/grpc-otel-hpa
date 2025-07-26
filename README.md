# GKE Autopilot gRPC 서버 HPA 구축 가이드 (OpenTelemetry Collector 사용)

이 가이드는 GKE Autopilot 클러스터에 배포된 gRPC 서버의 부하에 따라 Horizontal Pod Autoscaler(HPA)가 동작하도록 구성하는 전체 과정을 안내합니다. OpenTelemetry Collector를 중앙 집중식 메트릭 수집기로 사용하여 애플리케이션의 메트릭을 Google Cloud Managed Service for Prometheus(GMP)로 전송하고, 이 메트릭을 기반으로 HPA가 작동하도록 합니다.

### **전체 아키텍처 및 시나리오 요약**

1.  **GKE Autopilot 클러스터 생성:** Managed Service for Prometheus(GMP)가 활성화된 클러스터를 준비합니다.
2.  **gRPC 서버 개발:** OpenTelemetry로 계측된 스트리밍 gRPC 서버를 준비합니다.
3.  **컨테이너화 및 배포:** gRPC 서버를 컨테이너 이미지로 빌드합니다.
4.  **OpenTelemetry Collector 배포:** GKE 클러스터에 Google Cloud 기반의 OpenTelemetry Collector를 배포하고, 메트릭 타입 충돌을 방지하도록 설정을 수정합니다.
5.  **HPA 및 Gateway 배포:** OpenTelemetry를 통해 수집된 커스텀 메트릭을 기반으로 동작하는 `HPA`와, 외부 트래픽을 위한 GCLB를 생성하는 `Gateway` 및 `HTTPRoute`를 배포합니다.
6.  **테스트 실행 및 검증:** gRPC 클라이언트로 부하를 발생시키고, HPA가 수집된 메트릭을 기반으로 파드 수를 성공적으로 스케일링하는지 확인합니다.

### **제약 사항**

1.  **gRPC long-lived connection**: gRPC 스트림은 한 번 맺어진 TCP 연결 위에서 계속 통신하므로, HPA가 활성화되어 새로운 Pod가 추가되더라도 기존 연결들은 끊어지지 않고 계속 첫 번째 Pod에만 머무르게 됩니다. 새로운 Pod들은 클라이언트가 새로운 연결을 만들지 않는 한 아무런 트래픽도 받지 못하고 유휴 상태로 남게 됩니다. 이 가이드에서는 서버가 일정 시간 유지된 연결(grpc.max_connection_age)에 대해 클라이언트에 종료 신호(GOAWAY)를 보내면 클라이언트는 유예 시간(grpc.max_connection_age_grace) 동안 진행 중인 스트림을 완료한 후 다시 재연결하도록 구성했습니다. (참조문서: https://github.com/grpc/proposal/blob/master/A9-server-side-conn-mgt.md)

2.  **데이터 타입 충돌**: OpenTelemetry 라이브러리가 자동으로 생성하는 일부 메트릭은 Google Cloud의 관리형 Prometheus 수집기(GMP)가 요구하는 데이터 타입과 달라 충돌이 발생할 수 있습니다. 이 가이드에서는 OpenTelemetry Collector의 설정을 수정하여 이러한 문제를 해결합니다.

3.  **Google Managed Prometheus(GMP)**: GMP를 통해 Prometheus 형식으로 내보낸 커스텀 사용자 정의 측정 항목을 기준으로 HPA를 구성하려면 Promethus 측정 항목은 **Guage 유형**이어야 합니다. (참조문서: https://cloud.google.com/kubernetes-engine/docs/tutorials/autoscaling-metrics?hl=ko#custom-metric) (하지만, 테스트 해본 결과는 **Counter 유형**도 가능합니다.) 자동으로 생성되는 gRPC OpenTelemetry 서버 메트릭은 대부분 **Histogram 유형**이라서 HPA Custom Metric으로 사용이 불가합니다. 본 가이드에서는 서버로 들어오는 gRPC 활성 스트림 개수를 측정하는 Guage 유형의 Custom metric을 생성하여 사용합니다.  

---

### **Phase 1: GKE 클러스터 및 환경 준비**

1.  **gcloud 프로젝트 설정:**
    ```bash
    # (이미 설정하셨다면 생략)
    gcloud auth login
    gcloud config set project [YOUR_PROJECT_ID]
    gcloud config set compute/region [YOUR_REGION] # asia-northeast3
    ```

2.  **환경 변수 설정 및 확인:**
    ```bash
    export PROJECT_ID=$(gcloud config list --format 'value(core.project)')
    export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
    export REGION=$(gcloud config list --format 'value(compute.region)')
    export CLUSTER_NAME=[YOUR_CLUSTER_NAME] # grpc-otel-test

    echo "Project ID: $PROJECT_ID"
    echo "Project Number: $PROJECT_NUMBER"
    echo "Region: $REGION"
    echo "Cluster Name: $CLUSTER_NAME"
    ```

3.  **필수 API 활성화:**
    ```bash
    gcloud services enable \
        container.googleapis.com \
        monitoring.googleapis.com \
        artifactregistry.googleapis.com \
        cloudbuild.googleapis.com \
        --project=$PROJECT_ID
    ```

4.  **GKE Autopilot 클러스터 생성:**
    ```bash
    gcloud container clusters create-auto $CLUSTER_NAME \
        --release-channel=regular \
        --location=$REGION
    ```

5.  **Gateway API 기능 활성화:**
    ```bash
    gcloud container clusters update $CLUSTER_NAME \
        --location=$REGION \
        --gateway-api=standard
    ```

6.  **클러스터 인증 정보 가져오기:**
    ```bash
    gcloud container clusters get-credentials $CLUSTER_NAME --location $REGION
    kubectl config current-context
    ```

7.  **테스트용 TLS 인증서 생성:**
    ```bash
    # k8s 디렉토리로 이동
    cd ~/grpc-hpa-test/k8s
    
    # 자체 서명 인증서와 키 생성
    openssl req -x509 -newkey rsa:2048 -nodes -keyout tls.key -out tls.crt -subj "/CN=grpc.example.com"
    ```

---

### **Phase 2: 서버 애플리케이션 및 컨테이너화**

1.  **Artifact Registry 저장소 생성 (필요시):**
    ```bash
    gcloud artifacts repositories create grpc-test-repo \
        --repository-format=docker \
        --location=$REGION
    ```

2.  **Cloud Build로 이미지 빌드 및 푸시:**
    ```bash
    cd ~/grpc-hpa-test

    export IMAGE_TAG=$(date -u +%Y%m%d-%H%M%S)
    echo "New image tag: $IMAGE_TAG"
    
    gcloud builds submit ./server --tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/grpc-test-repo/vac-hub-test:${IMAGE_TAG}"
    ```

---

### **Phase 3: Google 기반 OpenTelemetry Collector 설치**

1.  **Collector를 위한 IAM 권한 구성:**
    ```bash
    gcloud projects add-iam-policy-binding projects/$PROJECT_ID \
        --role=roles/logging.logWriter \
        --member=principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/opentelemetry/sa/opentelemetry-collector
    gcloud projects add-iam-policy-binding projects/$PROJECT_ID \
        --role=roles/monitoring.metricWriter \
        --member=principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/opentelemetry/sa/opentelemetry-collector
    gcloud projects add-iam-policy-binding projects/$PROJECT_ID \
        --role=roles/cloudtrace.agent \
        --member=principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/opentelemetry/sa/opentelemetry-collector
    ```

2.  **Collector 기본 설정으로 배포:**
    ```bash
    kubectl kustomize https://github.com/GoogleCloudPlatform/otlp-k8s-ingest.git/k8s/base | envsubst | kubectl apply -f -
    ```

3.  **Collector 설정 수정:**
    ```bash
    cd ~/grpc-hpa-test/k8s
    kubectl apply -f ./collector-config.yaml
    ```

4.  **기존 Metric Descriptor 삭제 (필요시):**
    ```bash
    # app_messages_processed_count_total/counter 삭제
    curl -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" "https://monitoring.googleapis.com/v3/projects/$PROJECT_ID/metricDescriptors/prometheus.googleapis.com/app_messages_processed_count_total/counter"
    
    # otel_scope_info/gauge 삭제
    curl -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" "https://monitoring.googleapis.com/v3/projects/$PROJECT_ID/metricDescriptors/prometheus.googleapis.com/otel_scope_info/gauge"
    ```

---

### **Phase 4: Custom Metrics Stackdriver Adapter 설치**

1.  **Custom Metrics Stackdriver Adapter 배포:**
    ```bash
    kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/k8s-stackdriver/master/custom-metrics-stackdriver-adapter/deploy/production/adapter_new_resource_model.yaml
    ```

2.  **Adapter Pod 정상 동작 확인:**
    ```bash
    kubectl get pods -n custom-metrics
    ```

3.  **Custom Metrics API 등록 여부 확인:**
    ```bash
    kubectl get apiservices | grep custom.metrics.k8s.io
    ```

---

### **Phase 5: GKE 배포**

1.  **Namespace 및 TLS Secret 생성:**
    ```bash
    cd ~/grpc-hpa-test/k8s
    kubectl apply -f ./namespace.yaml
    kubectl create secret tls grpc-cert -n grpc-test --key tls.key --cert tls.crt --dry-run=client -o yaml | kubectl apply -f -
    ```

2.  **Gateway 및 HTTPRoute 생성:**
    ```bash
    cd ~/grpc-hpa-test/k8s
    kubectl apply -f ./gateway.yaml
    ```

3.  **Deployment 및 Service 생성:**
    ```bash
    cd ~/grpc-hpa-test/k8s
    envsubst < deployment.yaml | kubectl apply -f -
    kubectl apply -f ./service.yaml
    ```

4.  **HPA 생성:**
    ```bash
    cd ~/grpc-hpa-test/k8s
    kubectl apply -f ./hpa.yaml
    ```

---

### **Phase 6: 테스트 실행 및 결과 검증**

1.  **배포 상태 확인:**
    ```bash
    # Secret 확인
    kubectl get secret grpc-cert -n grpc-test

    # Deployment와 Service가 정상적으로 생성되었는지 확인
    kubectl get deployment,svc -n grpc-test

    # Pod 상태 확인
    kubectl get pods -n grpc-test
    ```
    ![배포 상태 확인 결과](./image/deployment_check.png)

2.  **Gateway Backend Protocol 및 외부 IP 확인:**
    *   Cloud Load Balancer backend protocol 확인
    ```bash
    gcloud compute backend-services list \
        --filter="name~grpc-test AND name~vac-hub-test-svc" \
        --format="value(name)" \
    | xargs -I {} gcloud compute backend-services describe {} --global --format="value(protocol)"
    ```
    ![Backend Protocol 확인 결과](./image/GW_Backend_protocol.png)

    *   GCLB가 프로비저닝되고 IP가 할당되기까지 몇 분 정도 소요됩니다.
    ```bash
    kubectl get gateway vac-hub-gateway -n grpc-test -w
    ```
    `ADDRESS` 필드에 나타나는 IP 주소를 복사합니다.

    ![GCLB 프로비저닝 결과](./image/GCLB_provisioning.png)

3.  **클라이언트 실행:**
    ```bash
    # 1. 서버 인증서 파일을 클라이언트 디렉토리로 복사
    cp ~/grpc-hpa-test/k8s/tls.crt ~/grpc-hpa-test/client/
    
    # 2. 가상환경 활성화 및 client 디렉토리로 이동
    cd ~/grpc-hpa-test
    source venv/bin/activate
    cd ~/grpc-hpa-test/client
    
    # 3. 클라이언트 실행 (스트림 수를 늘려 부하를 발생시킵니다)
    python client.py [GATEWAY_EXTERNAL_IP]:443 --streams 15 --cert_file ./tls.crt
    ```
    ![Client 실행 결과](./image/Client_running.png)

4.  **서버 POD 동작 확인:**
    *   새 터미널을 열고 첫번째 서버 POD의 변화를 실시간으로 확인합니다.
    ```bash
    kubectl get pods -n grpc-test
    kubectl logs -n grpc-test -f [FIRST_SERVER_POD_NAME]
    ```
    *  Client Stream이 10개까지 증가한 후 Stream 연결 시간 및 Grace Time이 지난 후 부터 grpc 부하가 줄어드는 것을 확인할 수 있습니다.
    ![첫번째 서버 POD 로그](./image/server_pod_logs.png)
    * GKE 콘솔의 워크로드 탭에서 vac-hub-test를 선택해서 확인하면 Client Stream이 유입된 후 HPA에 의해 POD 수평 확장이 일어났으나, 첫번째 POD에만 Connection이 연결되어 첫번째 POD는 비정상 작동하는 것을 확인할 수 있습니다. 이후 Connection이 POD들에 재분배되어 정상 작동하는 것을 확인할 수 있습니다. 
    ![gRPC 스트림이 유입된 직후 GKE 콘솔 상태](./image/gke_console_first.png)

    ![HPA 수평 확장 후 gRPC 스트림이 서버 POD에 골고루 분배된 이후 GKE 콘솔 상태](./image/gke_console_last.png)

5.  **HPA 동작 확인:**
    *   새 터미널을 열고 HPA의 변화를 실시간으로 확인합니다.
    ```bash
    
    kubectl get hpa vac-hub-test-hpa -n grpc-test -w
    ```
    *   `TARGETS` 컬럼에 `현재 값 / 목표 값` (예: `8/5`)이 표시됩니다. 현재 값이 목표 값을 초과하면 `REPLICAS` 수가 1에서 점차 늘어나는 것을 볼 수 있습니다.
    ![HPA 동작 확인 결과](./image/hpa_status.png)

6.  **Cloud Monitoring에서 메트릭 확인:**
    *   Google Cloud Console에서 **Monitoring > Metrics Explorer**로 이동합니다.
    *   **PROMQL** 탭을 선택하고 다음 쿼리를 입력합니다.
        ```promql
        grpc_server_active_streams{namespace="grpc-test"}
        ```
    *   그래프에 HPA에 의해 파드가 늘어나는 모습이 시각적으로 나타납니다.
    ![gRPC Server Active Stream 메트릭](./image/grpc_server_active_stream.png)

    *   **빌더** 탭을 선택하고 측정항목 선택을 클릭한 후 **Prometheus Target** > **Grpc** 항목 밑에 있는 Grpc 메트릭들을 선택해서 확인합니다. Opentelemetry에 의해 수집된 gRPC Server Metric을 확인할 수 있습니다.
    ![OTEL gRPC 메트릭 선택](./image/grpc_otel_metric_select.png)

    ![OTEL gRPC Server Duration 메트릭의 Heatmap 조회 결과 0~5초 사이에 98% 스트림이 위치하는 것을 확인](./image/otel_custom_metric_chart.png)

    *   **빌더** 탭을 선택하고 측정항목 선택을 클릭한 후 **Prometheus Target** > **App** > **app_message_processed_count_total**을 선택 한 후 확인합니다. Server App에서 넣었던 Custom Metric을 확인할 수 있습니다.
    ![Custom Metric 선택](./image/otel_custom_metric_select.png)
    
    ![서버에서 메세지 처리 갯수가 초당 140~150을 유지하는 것을 확인](./image/otel_custom_metric_chart.png)

7.  **테스트 종료 후 정리:**
    ```bash
    # Namespace 전체를 삭제하여 모든 리소스를 정리합니다.
    kubectl delete ns grpc-test

    # GKE 클러스터 삭제 (선택 사항)
    # gcloud container clusters delete $CLUSTER_NAME --location=$REGION
    ```