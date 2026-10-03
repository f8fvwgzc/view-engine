# GodView Engine

A local-first agent orchestration dashboard: create projects, add tasks, select agents, then watch a five-agent swarm coordinate in real time.

## Run locally

Start the complete stack:

```bash
docker compose up --build
```

Open `http://localhost:5173`. The Docker API is on `http://localhost:3002`, Postgres on `localhost:5432`, and Redis on `localhost:6379`. The frontend proxies API and WebSocket traffic automatically.

For local development outside Docker, start Postgres and Redis first:

```bash
docker compose up postgres redis
```

Then open two more terminals:

```bash
cd backend && cargo run
```

```bash
cd frontend && npm_config_cache=/tmp/godview-engine-npm-cache npm install && npm run dev
```

No authentication or API keys are required for the dashboard itself.

## Local Ollama workers

Ollama is the default agent provider. With your installed `gemma4:E4B` model,
start the stack with:

```bash
make up-ollama
```

The backend calls `http://host.docker.internal:11434` from Docker, which reaches
your local Ollama server without publishing it to the browser. The orchestrator
uses Gemma to select roles and every selected worker uses Gemma for its report.
Set `OLLAMA_MODEL` or `OLLAMA_URL` before running Compose to change the model or
server.

## Real Codex workers

The secure recommended local setup is a host-side Codex app-server with Docker
only connecting to it through `host.docker.internal`. The browser never reaches
the app-server and the capability token stays in `/private/tmp`.

```bash
make up-host-codex
```

Open `http://localhost:5173`, create a task, and dispatch it. The hidden
orchestrator uses Codex to select workers; each selected visible worker receives
its own isolated Codex thread. Its live response appears in the event stream and
the combined result appears under **Swarm Result** when the task finishes.

Useful commands:

```bash
make codex-status
make codex-logs
make down-host-codex
```

`make up-host-codex` binds the host app-server on port `4500` with a generated
capability token. Do not publish that port outside your private machine or
connect the frontend to it directly.

## What is included

- Axum API backed by Postgres; the first migration enables pgvector and creates the project, task, agent, and event tables.
- Five visible agents: one orchestrator plus planner, researcher, builder, and reviewer.
- Create a project and task, then dispatch it. The orchestrator reads the task brief, selects the relevant workers, and reveals each node as it dispatches that worker. Selected agents then work concurrently and exchange live cross-agent messages.
- Redis pub/sub relays every event to WebSocket clients, including the live event stream, agent-state refreshes, and browser notifications.
- The God View draws active dotted connections and moving light pulses for recent agent-to-agent messages. Select a completed task to read its persisted **Swarm Result** in the task inspector.
- `POST /api/events` accepts external events, which makes the UI ready for a separate LangGraph worker.

## LangGraph connection

LangGraph can run as a separate worker. Keep this Rust service as the source of truth for projects, tasks, agents, and the UI, then have every LangGraph node/tool `POST` an event to `http://localhost:3001/api/events` using the `SwarmEvent` JSON shape in `backend/src/main.rs`. Redis broadcasts it to every dashboard immediately and Postgres keeps the event history.

This project intentionally contains no Playwright tests or Rust test modules. Before exposing it publicly, add authentication and replace the local Docker credentials.

Project - Task runner Agents

Assigned tasks when start, god view engine tab side can see the how many agents working on that project/task and how exactly result comes? Employee/high role employees can see that agents running process

The tasks sequenced while - scheduler/worker/decider-task agent handle that whole project inside initially runs configure
Or the tasks not sequence is approved by PM, and during that sequenced tasks can modify able by Employee
Tasks can create in during project sprint process and tasks can extend the next sprint to move
And all tasks can just store in database is correct I hope or somewhere in store redis etc.

The orchestrator agent can decide the each agents behind work LLM/Codex - AI can multiple to configure
And all agents running results or that referring documentations/internet knowledges real time to scale the project knowledge.

Project - conversation page from that employees can realized demand create able /create-demand “demand name” and “realized process”
This employee created demands directly send to the High Role Employee that project handling and the project tab inside again decision tab create only for PM/ High Role Employee
And there HREmployee can approval that employees created demands and agents asking approvals for the each project

And Task assigned to Agent while agent directly ask to PM/High Role Employee for that project decision tab in agent want to start assigned “this named task” like approval asking and HREmployee can approve that. But assigned after immediately Agent run and what agent can do in this task something like steps explained and send to the approval to HREmployee. HighRoleEmployee can read and agent to set approval
