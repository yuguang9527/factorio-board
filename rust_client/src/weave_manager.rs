use crate::event_mediator::PlayerInfo;
use crate::weave_client::{
    EndedCallSchemaForInsert, StartedCallSchemaForInsert, WeaveClient, WeaveConfig,
};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use chrono::Utc;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::fs;
use tokio::sync::Mutex;
use uuid::Uuid;

/// A singleton service that manages Weave sessions for Factorio events.
/// Handles trace logging via start_call() and end_call() operations.
/// Weave sessions map 1:1 with WandB sessions using the same session_id.
pub struct WeaveManager {
    current_session_id: Arc<Mutex<Option<String>>>,
    active_calls: Arc<Mutex<HashMap<String, CallContext>>>,
    /// Cache for research events: key is "tech_name:tech_level", value is the call_id
    research_cache: Arc<Mutex<HashMap<String, String>>>,
    client: Arc<Mutex<Option<WeaveClient>>>,
    config: WeaveConfig,
    /// Active agent trajectory context (for Weave trace hierarchy)
    agent_trajectory: Arc<Mutex<Option<AgentTrajectoryContext>>>,
    /// Active agent step call_ids: step_number -> call_id
    agent_steps: Arc<Mutex<HashMap<u32, String>>>,
}

/// Context for an active Weave call/trace
#[derive(Debug, Clone)]
struct CallContext {
    call_id: String,
    trace_id: String,
    session_id: String,
    start_tick: u64,
    inputs: HashMap<String, String>,
}

/// Context for an active agent trajectory (for Weave trace hierarchy)
#[derive(Debug, Clone)]
struct AgentTrajectoryContext {
    call_id: String,
    trace_id: String,
}

impl WeaveManager {
    /// Creates a new Weave manager instance
    pub fn new() -> Self {
        // Load config from environment
        let config = match WeaveConfig::from_env() {
            Ok(cfg) => {
                println!(
                    "✅ Weave config loaded: entity={}, project={}",
                    cfg.entity, cfg.project
                );
                cfg
            }
            Err(e) => {
                eprintln!("⚠️  Failed to load Weave config: {}", e);
                eprintln!("⚠️  Weave integration will be disabled");
                // Create a dummy config - client won't be initialized
                WeaveConfig {
                    entity: "unknown".to_string(),
                    project: "unknown".to_string(),
                    base_url: "https://trace.wandb.ai".to_string(),
                    api_key: "dummy".to_string(),
                    binary_path: std::path::PathBuf::from("/dev/null"),
                    socket_path: std::path::PathBuf::from("/dev/null"),
                }
            }
        };

        WeaveManager {
            current_session_id: Arc::new(Mutex::new(None)),
            active_calls: Arc::new(Mutex::new(HashMap::new())),
            research_cache: Arc::new(Mutex::new(HashMap::new())),
            client: Arc::new(Mutex::new(None)),
            config,
            agent_trajectory: Arc::new(Mutex::new(None)),
            agent_steps: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Initialize the Weave client connection
    async fn ensure_client(&self) -> Result<(), String> {
        let mut client_guard = self.client.lock().await;

        if client_guard.is_some() {
            return Ok(());
        }

        let client = WeaveClient::new(self.config.clone());
        client.init().await?;

        *client_guard = Some(client);
        Ok(())
    }

    /// Handles a session_init event. Creates a new Weave session matching WandB.
    pub async fn handle_session_init(&self, session_id: String, tick: u64, level_name: String) {
        println!("🔷 Weave session init: {}", session_id);

        // End any active calls from previous session
        self.end_all_calls().await;

        // Clear research cache for new session
        self.research_cache.lock().await.clear();
        println!("🔷 Research cache cleared for new session");

        // Store new session ID
        *self.current_session_id.lock().await = Some(session_id.clone());

        // Ensure client is initialized
        if let Err(e) = self.ensure_client().await {
            eprintln!("⚠️  Failed to initialize Weave client: {}", e);
            return;
        }

        println!(
            "🔷 Weave session created: {} (tick: {}, level: {})",
            session_id, tick, level_name
        );

        // Log the session_init event as an atomic call
        let mut inputs = HashMap::new();
        inputs.insert("session_id".to_string(), serde_json::json!(&session_id));
        inputs.insert("tick".to_string(), serde_json::json!(tick));
        inputs.insert("level_name".to_string(), serde_json::json!(&level_name));

        let mut outputs = HashMap::new();
        outputs.insert("session_id".to_string(), serde_json::json!(session_id));
        outputs.insert("level_name".to_string(), serde_json::json!(level_name));

        self.log_call("session_init".to_string(), tick, inputs, outputs)
            .await;
    }

    /// Starts a new Weave call/trace
    pub async fn start_call(
        &self,
        call_id: String,
        operation: String,
        tick: u64,
        inputs: HashMap<String, String>,
    ) {
        // Ensure client is initialized (creates session if needed)
        if let Err(e) = self.ensure_client().await {
            eprintln!("⚠️  Failed to ensure Weave client: {}", e);
            return;
        }

        // Get active session
        let session_id = {
            let session_guard = self.current_session_id.lock().await;
            match session_guard.as_ref() {
                Some(id) => id.clone(),
                None => {
                    eprintln!("⚠️  Cannot start call '{}': no active Weave session", operation);
                    return;
                }
            }
        };

        // Now we're guaranteed to have a session_id
        // Generate UUIDs
        let weave_call_id = Uuid::now_v7().to_string();
        let trace_id = Uuid::now_v7().to_string();

        let context = CallContext {
            call_id: weave_call_id.clone(),
            trace_id: trace_id.clone(),
            session_id: session_id.clone(),
            start_tick: tick,
            inputs: inputs.clone(),
        };

        self.active_calls
            .lock()
            .await
            .insert(call_id.clone(), context);

        println!(
            "🔷 Weave call started: '{}' operation='{}' tick={} session={} weave_id={}",
            call_id, operation, tick, session_id, weave_call_id
        );

        // Convert string inputs to JSON and add session_id
        let mut inputs_json = HashMap::new();
        inputs_json.insert("session_id".to_string(), serde_json::json!(&session_id));
        for (k, v) in inputs.iter() {
            inputs_json.insert(k.clone(), serde_json::json!(v));
        }

        // Send to Weave
        if let Err(e) = self
            .send_start_call(weave_call_id, trace_id, session_id, operation, tick, inputs_json)
            .await
        {
            eprintln!("⚠️  Failed to send start call to Weave: {}", e);
        }
    }

    /// Sends a start call to Weave
    async fn send_start_call(
        &self,
        call_id: String,
        trace_id: String,
        session_id: String,
        operation: String,
        tick: u64,
        inputs: HashMap<String, serde_json::Value>,
    ) -> Result<(), String> {
        let client_guard = self.client.lock().await;
        let client = client_guard
            .as_ref()
            .ok_or_else(|| "Weave client not initialized".to_string())?;

        // Build attributes (metadata about the call)
        let mut attributes: HashMap<String, serde_json::Value> = HashMap::new();
        attributes.insert("tick".to_string(), serde_json::json!(tick));

        let start = StartedCallSchemaForInsert {
            project_id: self.config.project_id(),
            id: Some(call_id.clone()),
            op_name: operation,
            display_name: None,
            trace_id: Some(trace_id),
            parent_id: None,
            thread_id: Some(session_id),
            turn_id: Some(call_id),
            started_at: Utc::now(),
            attributes,
            inputs,
        };

        client.start_call(start).await
    }

    /// Ends an active Weave call/trace
    pub async fn end_call(
        &self,
        call_id: String,
        tick: u64,
        outputs: HashMap<String, String>,
        success: bool,
    ) {
        let mut active_calls = self.active_calls.lock().await;

        match active_calls.remove(&call_id) {
            None => {
                eprintln!("⚠️  Cannot end Weave call '{}': call not found", call_id);
            }
            Some(context) => {
                let duration_ticks = tick - context.start_tick;

                println!(
                    "🔷 Weave call ended: '{}' duration={} ticks success={} session={} weave_id={}",
                    call_id, duration_ticks, success, context.session_id, context.call_id
                );

                // Convert string outputs to JSON and add session_id
                let mut outputs_json = HashMap::new();
                outputs_json.insert("session_id".to_string(), serde_json::json!(&context.session_id));
                for (k, v) in outputs.iter() {
                    outputs_json.insert(k.clone(), serde_json::json!(v));
                }

                // Send to Weave
                drop(active_calls); // Release lock before async call
                if let Err(e) = self
                    .send_end_call(context.call_id, tick, duration_ticks, outputs_json, success)
                    .await
                {
                    eprintln!("⚠️  Failed to send end call to Weave: {}", e);
                }
            }
        }
    }

    /// Sends an end call to Weave
    async fn send_end_call(
        &self,
        call_id: String,
        tick: u64,
        duration_ticks: u64,
        outputs: HashMap<String, serde_json::Value>,
        success: bool,
    ) -> Result<(), String> {
        let client_guard = self.client.lock().await;
        let client = client_guard
            .as_ref()
            .ok_or_else(|| "Weave client not initialized".to_string())?;

        // Build output
        let mut output_map = outputs;
        output_map.insert("success".to_string(), serde_json::json!(success));
        output_map.insert("tick".to_string(), serde_json::json!(tick));

        // Build summary
        let mut summary: HashMap<String, serde_json::Value> = HashMap::new();
        summary.insert(
            "duration_ticks".to_string(),
            serde_json::json!(duration_ticks),
        );

        let end = EndedCallSchemaForInsert {
            project_id: self.config.project_id(),
            id: call_id,
            ended_at: Utc::now(),
            exception: if success {
                None
            } else {
                Some("Call failed".to_string())
            },
            output: Some(serde_json::to_value(output_map).unwrap()),
            summary,
        };

        client.end_call(end).await
    }

    /// Logs an atomic call to Weave (start and end at the same time).
    /// Useful for instant events that don't have duration.
    pub async fn log_call(
        &self,
        operation: String,
        tick: u64,
        inputs: HashMap<String, serde_json::Value>,
        outputs: HashMap<String, serde_json::Value>,
    ) {
        // Ensure client is initialized
        if let Err(e) = self.ensure_client().await {
            eprintln!("⚠️  Failed to ensure Weave client: {}", e);
            return;
        }

        // Get active session
        let session_id = {
            let session_guard = self.current_session_id.lock().await;
            match session_guard.as_ref() {
                Some(id) => id.clone(),
                None => {
                    eprintln!("⚠️  Cannot log Weave call '{}': no active session", operation);
                    return;
                }
            }
        };

        // Generate UUIDs
        let weave_call_id = Uuid::now_v7().to_string();
        let trace_id = Uuid::now_v7().to_string();

        println!(
            "🔷 Weave instant call: operation='{}' tick={} session={} weave_id={}",
            operation, tick, session_id, weave_call_id
        );

        // Add session_id to inputs and outputs
        let mut inputs_with_session = inputs;
        inputs_with_session.insert("session_id".to_string(), serde_json::json!(&session_id));

        let mut outputs_with_session = outputs;
        outputs_with_session.insert("session_id".to_string(), serde_json::json!(&session_id));

        // Send start and end calls
        if let Err(e) = self
            .send_start_call(
                weave_call_id.clone(),
                trace_id,
                session_id.clone(),
                operation.clone(),
                tick,
                inputs_with_session,
            )
            .await
        {
            eprintln!("⚠️  Failed to send start call to Weave: {}", e);
            return;
        }

        if let Err(e) = self
            .send_end_call(weave_call_id, tick, 0, outputs_with_session, true)
            .await
        {
            eprintln!("⚠️  Failed to send end call to Weave: {}", e);
        }
    }

    /// Handles research started event
    pub async fn handle_research_started(
        &self,
        tick: u64,
        tech_name: String,
        tech_level: u32,
    ) {
        let research_key = format!("{}:{}", tech_name, tech_level);

        let mut inputs = HashMap::new();
        inputs.insert("tech_name".to_string(), tech_name.clone());
        inputs.insert("tech_level".to_string(), tech_level.to_string());

        // Start a call and store the call_id in the research cache
        self.start_call(
            research_key.clone(),
            "research".to_string(),
            tick,
            inputs,
        )
        .await;
    }

    /// Handles research finished event
    pub async fn handle_research_finished(
        &self,
        tick: u64,
        tech_name: String,
        tech_level: u32,
    ) {
        let research_key = format!("{}:{}", tech_name, tech_level);

        let mut outputs = HashMap::new();
        outputs.insert("tech_name".to_string(), tech_name.clone());
        outputs.insert("tech_level".to_string(), tech_level.to_string());
        outputs.insert("completed".to_string(), "true".to_string());

        // End the call using the research key as call_id
        self.end_call(research_key, tick, outputs, true).await;
    }

    /// Handles entity built event
    pub async fn handle_entity_built(
        &self,
        tick: u64,
        player_index: u32,
        entity: String,
        position_x: f64,
        position_y: f64,
        surface: String,
    ) {
        let mut inputs = HashMap::new();
        inputs.insert("player_index".to_string(), serde_json::json!(player_index));
        inputs.insert("entity".to_string(), serde_json::json!(entity));
        inputs.insert("position_x".to_string(), serde_json::json!(position_x));
        inputs.insert("position_y".to_string(), serde_json::json!(position_y));
        inputs.insert("surface".to_string(), serde_json::json!(&surface));

        let mut outputs = HashMap::new();
        outputs.insert("entity".to_string(), serde_json::json!(entity));
        outputs.insert("surface".to_string(), serde_json::json!(surface));

        self.log_call("on_built_entity".to_string(), tick, inputs, outputs)
            .await;
    }

    /// Handles entity mined event
    pub async fn handle_entity_mined(
        &self,
        tick: u64,
        player_index: u32,
        entity: String,
        position_x: f64,
        position_y: f64,
        surface: String,
    ) {
        let mut inputs = HashMap::new();
        inputs.insert("player_index".to_string(), serde_json::json!(player_index));
        inputs.insert("entity".to_string(), serde_json::json!(entity));
        inputs.insert("position_x".to_string(), serde_json::json!(position_x));
        inputs.insert("position_y".to_string(), serde_json::json!(position_y));
        inputs.insert("surface".to_string(), serde_json::json!(&surface));

        let mut outputs = HashMap::new();
        outputs.insert("entity".to_string(), serde_json::json!(entity));
        outputs.insert("surface".to_string(), serde_json::json!(surface));

        self.log_call("on_player_mined_entity".to_string(), tick, inputs, outputs)
            .await;
    }

    /// Handles player crafted item event
    pub async fn handle_item_crafted(
        &self,
        tick: u64,
        player_index: u32,
        item: String,
        count: u32,
    ) {
        let mut inputs = HashMap::new();
        inputs.insert("player_index".to_string(), serde_json::json!(player_index));
        inputs.insert("item".to_string(), serde_json::json!(&item));
        inputs.insert("count".to_string(), serde_json::json!(count));

        let mut outputs = HashMap::new();
        outputs.insert("item".to_string(), serde_json::json!(item));
        outputs.insert("count".to_string(), serde_json::json!(count));

        self.log_call("on_player_crafted_item".to_string(), tick, inputs, outputs)
            .await;
    }

    /// Handles player snapshot event (from Stats)
    pub async fn handle_player_snapshot(
        &self,
        tick: u64,
        player_info: PlayerInfo,
        screenshot_path: String,
    ) {
        // Read the screenshot file and encode as base64
        let screenshot_data = match self.read_screenshot(&screenshot_path).await {
            Ok(data) => data,
            Err(e) => {
                eprintln!(
                    "⚠️  Failed to read screenshot at {}: {}",
                    screenshot_path, e
                );
                return;
            }
        };

        // Build inputs with player position and screenshot as data URI
        let mut inputs: HashMap<String, serde_json::Value> = HashMap::new();
        inputs.insert("position_x".to_string(), serde_json::json!(player_info.position.x));
        inputs.insert("position_y".to_string(), serde_json::json!(player_info.position.y));
        inputs.insert("surface".to_string(), serde_json::json!(player_info.surface));
        inputs.insert("health".to_string(), serde_json::json!(player_info.health));

        // Create Weave Image object format
        inputs.insert(
            "screenshot".to_string(),
            serde_json::json!({
                "_type": "Image",
                "data": screenshot_data
            })
        );

        // Build outputs with the same screenshot path
        let mut outputs: HashMap<String, serde_json::Value> = HashMap::new();
        outputs.insert("screenshot_path".to_string(), serde_json::json!(screenshot_path));

        // Log the call
        self.log_call("player_snapshot".to_string(), tick, inputs, outputs)
            .await;
    }

    /// Read screenshot file and encode as data URI
    async fn read_screenshot(&self, path: &str) -> Result<String, String> {
        // Get Factorio output directory from environment variable
        let factorio_output_dir = std::env::var("FACTORIO_OUTPUT_PATH")
            .map_err(|_| "FACTORIO_OUTPUT_PATH environment variable not set".to_string())?;

        let full_path = std::path::Path::new(&factorio_output_dir).join(path);

        let bytes = fs::read(&full_path)
            .await
            .map_err(|e| format!("Failed to read file {:?}: {}", full_path, e))?;

        let base64_data = BASE64.encode(&bytes);
        Ok(format!("data:image/png;base64,{}", base64_data))
    }

    // ─── Agent trace methods (FLE benchmark integration) ─────────────

    /// Start a Weave call with explicit trace_id and parent_id (for agent hierarchy)
    async fn start_traced_call(
        &self,
        operation: String,
        inputs: HashMap<String, serde_json::Value>,
        trace_id: String,
        parent_id: Option<String>,
    ) -> Option<String> {
        if let Err(e) = self.ensure_client().await {
            eprintln!("⚠️  Failed to ensure Weave client: {}", e);
            return None;
        }

        let session_id = {
            let guard = self.current_session_id.lock().await;
            match guard.as_ref() {
                Some(id) => id.clone(),
                None => {
                    eprintln!("⚠️  Cannot start traced call '{}': no active session", operation);
                    return None;
                }
            }
        };

        let call_id = Uuid::now_v7().to_string();

        let client_guard = self.client.lock().await;
        let client = match client_guard.as_ref() {
            Some(c) => c,
            None => return None,
        };

        let mut inputs_with_session = inputs;
        inputs_with_session.insert("session_id".to_string(), serde_json::json!(&session_id));

        let start = StartedCallSchemaForInsert {
            project_id: self.config.project_id(),
            id: Some(call_id.clone()),
            op_name: operation,
            display_name: None,
            trace_id: Some(trace_id),
            parent_id,
            thread_id: Some(session_id),
            turn_id: Some(call_id.clone()),
            started_at: Utc::now(),
            attributes: HashMap::new(),
            inputs: inputs_with_session,
        };

        if let Err(e) = client.start_call(start).await {
            eprintln!("⚠️  Failed to send traced start call: {}", e);
            return None;
        }

        Some(call_id)
    }

    /// End a traced call
    async fn end_traced_call(
        &self,
        call_id: &str,
        outputs: HashMap<String, serde_json::Value>,
        success: bool,
    ) {
        let client_guard = self.client.lock().await;
        let client = match client_guard.as_ref() {
            Some(c) => c,
            None => return,
        };

        let end = EndedCallSchemaForInsert {
            project_id: self.config.project_id(),
            id: call_id.to_string(),
            ended_at: Utc::now(),
            exception: if success { None } else { Some("Error".to_string()) },
            output: Some(serde_json::to_value(&outputs).unwrap()),
            summary: HashMap::new(),
        };

        if let Err(e) = client.end_call(end).await {
            eprintln!("⚠️  Failed to send traced end call: {}", e);
        }
    }

    /// Log an atomic traced call (start + end immediately)
    async fn log_traced_call(
        &self,
        operation: String,
        inputs: HashMap<String, serde_json::Value>,
        outputs: HashMap<String, serde_json::Value>,
        trace_id: String,
        parent_id: Option<String>,
    ) {
        if let Some(call_id) = self
            .start_traced_call(operation, inputs, trace_id, parent_id)
            .await
        {
            self.end_traced_call(&call_id, outputs, true).await;
        }
    }

    /// Log a traced call with explicit duration (started_at back-dated from latency)
    async fn log_traced_call_with_duration(
        &self,
        operation: String,
        inputs: HashMap<String, serde_json::Value>,
        outputs: HashMap<String, serde_json::Value>,
        trace_id: String,
        parent_id: Option<String>,
        duration_ms: i64,
    ) {
        if let Err(e) = self.ensure_client().await {
            eprintln!("⚠️  Failed to ensure Weave client: {}", e);
            return;
        }

        let session_id = {
            let guard = self.current_session_id.lock().await;
            match guard.as_ref() {
                Some(id) => id.clone(),
                None => return,
            }
        };

        let call_id = Uuid::now_v7().to_string();
        let ended_at = Utc::now();
        let started_at = ended_at - chrono::Duration::milliseconds(duration_ms);

        let client_guard = self.client.lock().await;
        let client = match client_guard.as_ref() {
            Some(c) => c,
            None => return,
        };

        let mut inputs_with_session = inputs;
        inputs_with_session.insert("session_id".to_string(), serde_json::json!(&session_id));

        let start = StartedCallSchemaForInsert {
            project_id: self.config.project_id(),
            id: Some(call_id.clone()),
            op_name: operation,
            display_name: None,
            trace_id: Some(trace_id),
            parent_id,
            thread_id: Some(session_id),
            turn_id: Some(call_id.clone()),
            started_at,
            attributes: HashMap::new(),
            inputs: inputs_with_session,
        };

        if let Err(e) = client.start_call(start).await {
            eprintln!("⚠️  Failed to send traced start call: {}", e);
            return;
        }

        let end = EndedCallSchemaForInsert {
            project_id: self.config.project_id(),
            id: call_id,
            ended_at,
            exception: None,
            output: Some(serde_json::to_value(&outputs).unwrap()),
            summary: HashMap::new(),
        };

        if let Err(e) = client.end_call(end).await {
            eprintln!("⚠️  Failed to send traced end call: {}", e);
        }
    }

    /// Dispatch agent events from the pipe
    pub async fn handle_agent_event(
        &self,
        event_name: &str,
        data: &HashMap<String, serde_json::Value>,
    ) {
        match event_name {
            "trajectory_start" => self.handle_trajectory_start(data).await,
            "step_start" => self.handle_step_start(data).await,
            "llm_response" => self.handle_llm_response(data).await,
            "code_result" => self.handle_code_result(data).await,
            "screenshot" => self.handle_screenshot(data).await,
            "trajectory_end" => self.handle_trajectory_end(data).await,
            _ => eprintln!("⚠️  Unknown agent event: {}", event_name),
        }
    }

    async fn handle_trajectory_start(&self, data: &HashMap<String, serde_json::Value>) {
        let trace_id = Uuid::now_v7().to_string();

        let mut inputs = HashMap::new();
        for key in ["model", "task", "max_steps"] {
            if let Some(v) = data.get(key) {
                inputs.insert(key.to_string(), v.clone());
            }
        }

        if let Some(call_id) = self
            .start_traced_call("trajectory".to_string(), inputs, trace_id.clone(), None)
            .await
        {
            *self.agent_trajectory.lock().await = Some(AgentTrajectoryContext {
                call_id,
                trace_id,
            });
            self.agent_steps.lock().await.clear();
            println!("🤖 Trajectory started");
        }
    }

    async fn handle_step_start(&self, data: &HashMap<String, serde_json::Value>) {
        let traj = self.agent_trajectory.lock().await.clone();
        let Some(traj) = traj else {
            eprintln!("⚠️  step_start without active trajectory");
            return;
        };

        let step = data
            .get("step")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;

        let mut inputs = HashMap::new();
        inputs.insert("step".to_string(), serde_json::json!(step));
        if let Some(v) = data.get("observation") {
            inputs.insert("observation".to_string(), v.clone());
        }

        if let Some(call_id) = self
            .start_traced_call(
                format!("step_{}", step),
                inputs,
                traj.trace_id,
                Some(traj.call_id),
            )
            .await
        {
            self.agent_steps.lock().await.insert(step, call_id);
            println!("🤖 Step {} started", step);
        }
    }

    async fn handle_llm_response(&self, data: &HashMap<String, serde_json::Value>) {
        let traj = self.agent_trajectory.lock().await.clone();
        let Some(traj) = traj else { return };

        let step = data
            .get("step")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;
        let step_call_id = self.agent_steps.lock().await.get(&step).cloned();

        let latency_ms = data
            .get("latency_ms")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let mut inputs = HashMap::new();
        inputs.insert("tokens_in".to_string(), data.get("tokens_in").cloned().unwrap_or(serde_json::json!(0)));

        let mut outputs = HashMap::new();
        for key in ["reasoning", "code"] {
            if let Some(v) = data.get(key) {
                outputs.insert(key.to_string(), v.clone());
            }
        }
        outputs.insert("tokens_out".to_string(), data.get("tokens_out").cloned().unwrap_or(serde_json::json!(0)));
        outputs.insert("latency_ms".to_string(), serde_json::json!(latency_ms));

        self.log_traced_call_with_duration(
            "llm_call".to_string(),
            inputs,
            outputs,
            traj.trace_id,
            step_call_id,
            latency_ms,
        )
        .await;
        println!("🤖 LLM response logged for step {} ({}ms)", step, latency_ms);
    }

    async fn handle_code_result(&self, data: &HashMap<String, serde_json::Value>) {
        let traj = self.agent_trajectory.lock().await.clone();
        let Some(traj) = traj else { return };

        let step = data
            .get("step")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;
        let step_call_id = self.agent_steps.lock().await.get(&step).cloned();

        let mut inputs = HashMap::new();
        if let Some(v) = data.get("code") {
            inputs.insert("code".to_string(), v.clone());
        }

        let mut outputs = HashMap::new();
        for key in ["output", "error"] {
            if let Some(v) = data.get(key) {
                outputs.insert(key.to_string(), v.clone());
            }
        }
        let production_score = data.get("production_score").cloned().unwrap_or(serde_json::json!(0));
        outputs.insert("production_score".to_string(), production_score.clone());

        // Log the code_exec span with 500ms duration (RCON execution time)
        self.log_traced_call_with_duration(
            "code_exec".to_string(),
            inputs,
            outputs.clone(),
            traj.trace_id.clone(),
            step_call_id.clone(),
            500, // RCON exec is fast, ~500ms
        )
        .await;

        // End the parent step span with summary
        if let Some(step_id) = step_call_id {
            let has_error = data
                .get("error")
                .map(|v| !v.is_null() && v.as_str() != Some(""))
                .unwrap_or(false);
            let mut step_outputs = HashMap::new();
            step_outputs.insert("production_score".to_string(), production_score);
            if has_error {
                step_outputs.insert("error".to_string(), data.get("error").cloned().unwrap_or(serde_json::json!(null)));
            }
            if let Some(v) = data.get("output") {
                step_outputs.insert("result".to_string(), v.clone());
            }
            self.end_traced_call(&step_id, step_outputs, !has_error).await;
            self.agent_steps.lock().await.remove(&step);
        }
        println!("🤖 Code result logged for step {}", step);
    }

    async fn handle_screenshot(&self, data: &HashMap<String, serde_json::Value>) {
        let traj = self.agent_trajectory.lock().await.clone();
        let Some(traj) = traj else { return };

        let step = data
            .get("step")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;

        let screenshot_path = match data.get("screenshot_path").and_then(|v| v.as_str()) {
            Some(p) => p.to_string(),
            None => return,
        };

        // Read screenshot from Factorio's script-output directory
        let full_path = {
            let output_path = std::env::var("FACTORIO_OUTPUT_PATH").unwrap_or_default();
            std::path::PathBuf::from(output_path).join(&screenshot_path)
        };

        let screenshot_data = match self.read_screenshot(&screenshot_path).await {
            Ok(data) => data,
            Err(e) => {
                // Try with full path
                match tokio::fs::read(&full_path).await {
                    Ok(bytes) => {
                        use base64::Engine;
                        let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
                        format!("data:image/png;base64,{}", b64)
                    }
                    Err(_) => {
                        eprintln!("⚠️  Screenshot not found: {} ({})", screenshot_path, e);
                        return;
                    }
                }
            }
        };

        let step_call_id = self.agent_steps.lock().await.get(&step).cloned();

        let mut inputs = HashMap::new();
        inputs.insert("step".to_string(), serde_json::json!(step));
        inputs.insert(
            "screenshot".to_string(),
            serde_json::json!({"_type": "image-file", "data": screenshot_data}),
        );

        let outputs = HashMap::new();

        self.log_traced_call(
            "screenshot".to_string(),
            inputs,
            outputs,
            traj.trace_id,
            step_call_id,
        )
        .await;
        println!("📸 Screenshot logged for step {}", step);
    }

    async fn handle_trajectory_end(&self, data: &HashMap<String, serde_json::Value>) {
        let traj = self.agent_trajectory.lock().await.take();
        let Some(traj) = traj else {
            eprintln!("⚠️  trajectory_end without active trajectory");
            return;
        };

        let mut outputs = HashMap::new();
        for key in ["total_steps", "final_score", "reason"] {
            if let Some(v) = data.get(key) {
                outputs.insert(key.to_string(), v.clone());
            }
        }

        self.end_traced_call(&traj.call_id, outputs, true).await;
        self.agent_steps.lock().await.clear();
        println!("🤖 Trajectory ended");
    }

    /// Ends all active calls (used during session transitions)
    async fn end_all_calls(&self) {
        // First, collect all calls to end
        let calls_to_end: Vec<CallContext> = {
            let mut active_calls = self.active_calls.lock().await;
            let call_count = active_calls.len();

            if call_count > 0 {
                println!(
                    "🔷 Ending {} active Weave calls due to session change",
                    call_count
                );
                active_calls.drain().map(|(_, context)| context).collect()
            } else {
                Vec::new()
            }
        };

        // Now end each call without holding the lock
        for context in calls_to_end {
            println!(
                "🔷 Force-ending Weave call: '{}' session={} weave_id={}",
                context.call_id, context.session_id, context.call_id
            );

            // Force end the call with failure
            if let Err(e) = self
                .send_end_call(
                    context.call_id,
                    context.start_tick,
                    0,
                    HashMap::new(),
                    false,
                )
                .await
            {
                eprintln!("⚠️  Failed to force-end call: {}", e);
            }
        }
    }

    /// Returns the count of currently active calls
    pub async fn active_call_count(&self) -> usize {
        self.active_calls.lock().await.len()
    }

    /// Checks if a specific call is active
    pub async fn is_call_active(&self, call_id: &str) -> bool {
        self.active_calls.lock().await.contains_key(call_id)
    }

    /// Public method to explicitly close the current session (e.g., on shutdown)
    pub async fn shutdown(&self) {
        println!("🔷 Shutting down Weave manager...");
        self.end_all_calls().await;
        *self.current_session_id.lock().await = None;

        // Flush and shutdown client
        let client_guard = self.client.lock().await;
        if let Some(client) = client_guard.as_ref() {
            if let Err(e) = client.wait_idle().await {
                eprintln!("⚠️  Failed to wait for idle: {}", e);
            }
            if let Err(e) = client.shutdown().await {
                eprintln!("⚠️  Failed to shutdown client: {}", e);
            }
        }
        drop(client_guard);

        println!("🔷 Weave manager shutdown complete");
    }
}

impl Drop for WeaveManager {
    fn drop(&mut self) {
        // Note: We can't call async shutdown from Drop
        // The user should call shutdown() explicitly before dropping
        println!("⚠️  WeaveManager dropped - ensure shutdown() was called first");
    }
}
