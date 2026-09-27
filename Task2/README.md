# Задание 2. Динамическое масштабирование контейнеров

Тестовое приложение — `ghcr.io/yandex-practicum/scaletestapp` (Go, порт `8080`):
- `GET /` — возвращает имя пода;
- `GET /metrics` — метрики Prometheus (счётчик `http_requests_total`).

## Содержимое

| Файл | Назначение |
|---|---|
| `deployment.yaml` | Deployment: 1 реплика, `requests.memory: 8Mi`, `limits.memory: 30Mi` |
| `service.yaml` | Service `scaletestapp` (порт `http:8080`) |
| `hpa-memory.yaml` | HPA по памяти: `averageUtilization: 80`, 1..10 реплик |
| `hpa-rps.yaml` | HPA по RPS: `http_requests_per_second`, `AverageValue: 10`, 1..10 реплик |
| `servicemonitor.yaml` | ServiceMonitor для сбора метрик Prometheus |
| `prometheus-adapter-values.yaml` | values для prometheus-adapter: кастомная метрика RPS |
| `locustfile.py` | Сценарий нагрузочного теста Locust |
| `locustfile-memory.py` | Дополнительный сценарий без пауз между запросами — создаёт давление на память |
| `logs/` | Логи и скриншоты (см. ниже) |

## Часть 1. HPA по утилизации памяти

Кластер — Kubernetes из Docker Desktop (вместо Minikube). В Docker Desktop metrics-server не входит
в поставку, поэтому он установлен в кластер отдельно.

1. Установлен metrics-server (с флагом `--kubelet-insecure-tls` — у kubelet самоподписанный сертификат).
2. Развёрнуто приложение: 1 реплика, лимит памяти `30Mi`.
3. Создан HPA по памяти с целевой утилизацией `80 %`, 1..10 реплик.

```bash
kubectl apply -f deployment.yaml service.yaml hpa-memory.yaml
kubectl get hpa scaletestapp-hpa-memory
```

**Про `requests.memory`.** HPA считает утилизацию памяти в процентах от `requests`, а не от `limits`.
Если задать `requests = limits = 30Mi`, порог 80 % — это 24Mi, а приложение в покое потребляет ~2.6Mi,
под нагрузкой — 10..23Mi: порог почти не достигается, и HPA не реагирует. Поэтому request выставлен
по реальному профилю потребления — `8Mi` (порог 80 % = 6.4Mi), а лимит оставлен `30Mi`, как требует
задание.

**Результат.** Под нагрузкой (1000 пользователей, `locustfile-memory.py` — без пауз между запросами)
утилизация памяти выросла до 128..207 % от request, и HPA поднял число реплик **1 → 2 → 4 → 6 → 10**:

```
New size: 2;  reason: memory resource utilization (percentage of request) above target
New size: 4;  reason: memory resource utilization (percentage of request) above target
New size: 6;  reason: memory resource utilization (percentage of request) above target
New size: 10; reason: memory resource utilization (percentage of request) above target
```

На пике, до момента scale-out, два пода успели получить `OOMKilled` (exit 137) — одиночный под не
успевал за ростом нагрузки в пределах лимита `30Mi`; после выхода на 10 реплик рестартов больше нет.
Замеры утилизации и числа реплик — в `logs/hpa-memory-under-load.txt`, состояние и события — в
`logs/hpa-memory-describe.txt`.

> Оба HPA нацелены на один Deployment, поэтому одновременно в кластере держится только один из них:
> для части 1 применяется `hpa-memory.yaml`, для части 2 — `hpa-rps.yaml`.

## Часть 2. HPA по количеству запросов в секунду (RPS)

Для масштабирования по RPS используется внешняя метрика из Prometheus, передаваемая в
Kubernetes через prometheus-adapter.

1. Установлен `kube-prometheus-stack` (Prometheus Operator + Prometheus).

```bash
helm install prometheus prometheus-community/kube-prometheus-stack
```

2. Метрики приложения собираются через `ServiceMonitor` (лейбл `release: prometheus`).

```bash
kubectl apply -f servicemonitor.yaml
```

3. Установлен `prometheus-adapter` с правилом, которое превращает счётчик
   `http_requests_total` в RPS-метрику:

```bash
helm install prometheus-adapter prometheus-community/prometheus-adapter \
  -f prometheus-adapter-values.yaml
```

```yaml
seriesQuery: 'http_requests_total{namespace!="",pod!=""}'
name:
  matches: "^http_requests_total$"
  as: "http_requests_per_second"
metricsQuery: 'sum(rate(http_requests_total{<<.LabelMatchers>>}[1m])) by (<<.GroupBy>>)'
```

4. Проверка, что метрика доступна в Kubernetes:

```bash
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/http_requests_per_second
```

5. Создан HPA по кастомной метрике: цель **10 запросов/с на под**, 1..10 реплик.

```bash
kubectl apply -f hpa-rps.yaml
```

6. Нагрузка генерировалась Locust'ом (600 пользователей, сценарий `locustfile.py`).

```bash
locust -f locustfile.py --headless --host http://scaletestapp:8080 -u 600 -r 100
```

**Результат.** Под нагрузкой ~200 RPS HPA поднял число реплик с 1 до 10:

```
New size: 4;  reason: pods metric http_requests_per_second above target
New size: 8;  reason: pods metric http_requests_per_second above target
New size: 10; reason: pods metric http_requests_per_second above target
```

Подробности — в `logs/hpa-rps-scale.txt` и `logs/hpa-rps-describe.txt`.

## Evidence (каталог `logs/`)

- `hpa-memory-describe.txt` — состояние HPA по памяти и события масштабирования 1→2→4→6→10.
- `hpa-memory-under-load.txt` — замеры потребления памяти подами и числа реплик во время нагрузки.
- `hpa-rps-scale.txt` — рост реплик 1→4→8→10 во время нагрузки (замеры каждые 15 с).
- `hpa-rps-describe.txt` — события масштабирования и целевая метрика.
- `locust-load.log` — отчёт Locust (~200 RPS, 0 ошибок).
- `locust-memory-run.txt` — первый прогон для части 1 (600 пользователей с паузами 1–5 с): память
  оставалась ~2-4Mi и HPA не масштабировал — отсюда вывод, что `requests.memory` нужно задавать
  по реальному профилю потребления.
- `prometheus-targets.txt` — цель `scaletestapp` в Prometheus (`up`), 10 подов.
- `prometheus-rate-query.txt` — `sum(rate(http_requests_total[1m]))` ≈ 200 RPS.
- `custom-metrics-api.txt` — метрика `http_requests_per_second` для 10 подов.
- `prometheus-graph.png` — график `sum(rate(http_requests_total[1m]))` за окно нагрузки: выход на плато ~200 RPS и спад после её окончания.
- `prometheus-targets.png` — цель ServiceMonitor `scaletestapp` в Prometheus: `10 / 10 up` (по одной на каждый под).
