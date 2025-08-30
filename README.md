# grafana-alerts-board

# for docker installation

- change .env variables

```
git clone https://github.com/imanbakhtiari/grafana-alerts-board.git
cd grafana-alerts-board
sudo docker compose up -d --build
```

# 🛰️ DC Alerts — Unified Multi-Grafana Alert Manager

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

<img src="https://www.apache.org/img/asf_logo.png" width="220" alt="Apache Logo"/>

---

## 📖 Overview

**DC Alerts** is an open-source monitoring dashboard that connects to **multiple Grafana instances** (per data center / PoP site), fetches alerts, and aggregates them into a **single unified view**.

It helps NOC/SRE teams quickly see **active vs. silenced alerts** across distributed environments, manage silences directly, and generate historical reports.

---

## ✨ Features

- 🔗 Connect to **multiple Grafana instances** via API  
- 🗂️ Aggregate alerts **per DC (data center / PoP)**  
- ✅ View **active** and **silenced** alerts side-by-side  
- ✍️ Create, edit, and unsilence alerts directly from the UI  
- 📊 Daily, weekly, and monthly **alert reports**  
- 💾 Store historical snapshots in a SQLite DB  
- 🌐 REST API for integrations (`/api/alerts`, `/api/silence`, `/api/report/...`)  
- 🐳 Easy deployment with **Docker & Docker Compose**

---

## 🏗️ Architecture

- **Frontend:**  
  - HTML5 + Bootstrap 5  
  - Unified UI to list alerts, toggle silences, and view per-DC dashboards

- **Backend:**  
  - Python 3.11 + Flask  
  - Periodically polls Grafana `/api/alertmanager/v2/alerts` and `/api/v2/silences`  
  - Normalizes alerts, detects DC from labels/annotations, persists snapshots  

- **Database:**  
  - SQLite (via SQLAlchemy ORM)  
  - Tables:
    - `dc_counts` – DC-level stats per snapshot  
    - `alert_snapshots` – Detailed alert records for reports  

---

```
.
├── app.py           # Flask entrypoint
├── db.py            # SQLAlchemy models (SQLite)
├── templates/       # HTML templates
├── static/          # JS/CSS/icons
├── Dockerfile
├── docker-compose.yml
└── README.md
```

API Endpoints

- GET /api/alerts – aggregated alerts per DC
- POST /api/silence – create or edit silence
- POST /api/unsilence – unsilence
- GET /api/report/daily – daily report JSON
- GET /api/report/weekly – weekly report JSON
- GET /api/report/monthly – monthly report JSON
- GET /healthz – health check

Copyright 2025 Your Name / Your Org

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

