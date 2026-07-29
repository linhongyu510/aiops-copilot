USE observability;

CREATE TABLE IF NOT EXISTS service_alerts (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    alert_name VARCHAR(128) NOT NULL,
    severity ENUM('warning', 'critical') NOT NULL,
    service_name VARCHAR(128) NOT NULL,
    status ENUM('firing', 'resolved') NOT NULL,
    summary VARCHAR(512) NOT NULL,
    started_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_alert_service_status (service_name, status),
    INDEX idx_alert_updated_at (updated_at)
);

CREATE TABLE IF NOT EXISTS service_metrics (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    service_name VARCHAR(128) NOT NULL,
    metric_name VARCHAR(128) NOT NULL,
    metric_value DECIMAL(12, 4) NOT NULL,
    metric_unit VARCHAR(32) NOT NULL,
    collected_at DATETIME NOT NULL,
    INDEX idx_metric_lookup (service_name, metric_name, collected_at)
);

CREATE TABLE IF NOT EXISTS incident_history (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    service_name VARCHAR(128) NOT NULL,
    incident_type VARCHAR(64) NOT NULL,
    root_cause VARCHAR(512) NOT NULL,
    resolution VARCHAR(1024) NOT NULL,
    duration_minutes INT NOT NULL,
    occurred_at DATETIME NOT NULL,
    INDEX idx_incident_service_time (service_name, occurred_at)
);

INSERT INTO service_alerts
    (alert_name, severity, service_name, status, summary, started_at, updated_at)
VALUES
    ('HighCPUUsage', 'critical', 'order-service', 'firing', 'CPU usage above 85% for 10 minutes', NOW() - INTERVAL 18 MINUTE, NOW()),
    ('SlowResponse', 'warning', 'gateway', 'firing', 'P95 latency above 1200 ms', NOW() - INTERVAL 12 MINUTE, NOW()),
    ('HighMemoryUsage', 'warning', 'payment-service', 'resolved', 'Memory usage above 80%', NOW() - INTERVAL 2 HOUR, NOW() - INTERVAL 70 MINUTE);

INSERT INTO service_metrics
    (service_name, metric_name, metric_value, metric_unit, collected_at)
VALUES
    ('order-service', 'cpu_usage', 91.5000, 'percent', NOW() - INTERVAL 2 MINUTE),
    ('order-service', 'memory_usage', 63.2000, 'percent', NOW() - INTERVAL 2 MINUTE),
    ('gateway', 'p95_latency', 1460.0000, 'millisecond', NOW() - INTERVAL 1 MINUTE),
    ('gateway', 'error_rate', 4.8000, 'percent', NOW() - INTERVAL 1 MINUTE);

INSERT INTO incident_history
    (service_name, incident_type, root_cause, resolution, duration_minutes, occurred_at)
VALUES
    ('order-service', 'cpu', 'Unbounded batch job consumed all worker threads', 'Limited batch concurrency and added CPU circuit breaker', 37, NOW() - INTERVAL 21 DAY),
    ('gateway', 'latency', 'Downstream inventory timeout caused request accumulation', 'Enabled timeout isolation and reduced retry count', 42, NOW() - INTERVAL 14 DAY);

-- The image creates MYSQL_USER before running this script. Restrict it to SELECT.
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'oncall_reader'@'%';
GRANT SELECT ON observability.* TO 'oncall_reader'@'%';
FLUSH PRIVILEGES;
