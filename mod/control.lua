-- Import utility functions
local utils = require("utils")

-- Generate a new session ID using level name and current tick
local function generate_session_id()
  local level_name = script.level.level_name or "unknown"
  local current_tick = game.tick
  return level_name .. "_" .. current_tick
end

-- Local flag to track if we've regenerated session after load
local session_regenerated = false

-- Initialize session ID on new game
script.on_init(function()
  storage.session_id = generate_session_id()
  storage.last_tick = game.tick
end)

-- On load, set local flag to regenerate session
script.on_load(function()
  session_regenerated = false
end)

-- On first tick after load, regenerate session ID
local function check_and_regenerate_session()
  if not session_regenerated then
    local old_session = storage.session_id
    storage.session_id = generate_session_id()
    storage.last_tick = game.tick
    session_regenerated = true

    -- Send session init event to named pipe
    local init_event = {
      type = "session_init",
      session_id = storage.session_id,
      tick = game.tick,
      level_name = script.level.level_name or "unknown"
    }
    local json_str = helpers.table_to_json(init_event)
    helpers.write_file("events.pipe", json_str .. "\n", true)

    -- Debug output
    game.print("Session ID regenerated: " .. (old_session or "none") .. " -> " .. storage.session_id)
  end
end

-- Event handler for when a player builds/places an entity
script.on_event(defines.events.on_built_entity, function(event)
  check_and_regenerate_session()
  local entity = event.entity
  local player = game.players[event.player_index]

  if entity and player then
    local event_data = {
      type = "event",
      event_name = "on_built_entity",
      session_id = storage.session_id,
      tick = event.tick,
      player_index = event.player_index,
      entity = entity.name,
      position = {x = entity.position.x, y = entity.position.y},
      surface = entity.surface.name
    }
    local json_str = helpers.table_to_json(event_data)
    helpers.write_file("events.pipe", json_str .. "\n", true)
  end
end)

-- Event handler for when a player mines/removes an entity
script.on_event(defines.events.on_player_mined_entity, function(event)
  check_and_regenerate_session()
  local entity = event.entity
  
  if entity then
    local event_data = {
      type = "event",
      event_name = "on_player_mined_entity",
      session_id = storage.session_id,
      tick = event.tick,
      player_index = event.player_index,
      entity = entity.name,
      position = {x = entity.position.x, y = entity.position.y},
      surface = entity.surface.name
    }
    local json_str = helpers.table_to_json(event_data)
    helpers.write_file("events.pipe", json_str .. "\n", true)
  end
end)

-- Event handler for research started
script.on_event(defines.events.on_research_started, function(event)
  check_and_regenerate_session()
  local research = event.research
  
  local event_data = {
    type = "event",
    event_name = "on_research_started",
    session_id = storage.session_id,
    tick = event.tick,
    tech_name = research.name,
    tech_level = research.level
  }
  local json_str = helpers.table_to_json(event_data)
  helpers.write_file("events.pipe", json_str .. "\n", true)
end)

-- Event handler for research completed
script.on_event(defines.events.on_research_finished, function(event)
  check_and_regenerate_session()
  local research = event.research
  
  local event_data = {
    type = "event",
    event_name = "on_research_finished",
    session_id = storage.session_id,
    tick = event.tick,
    tech_name = research.name,
    tech_level = research.level
  }
  local json_str = helpers.table_to_json(event_data)
  helpers.write_file("events.pipe", json_str .. "\n", true)
end)

-- Event handler for player crafted item
script.on_event(defines.events.on_player_crafted_item, function(event)
  check_and_regenerate_session()
  
  local event_data = {
    type = "event",
    event_name = "on_player_crafted_item",
    session_id = storage.session_id,
    tick = event.tick,
    player_index = event.player_index,
    item = event.item_stack.name,
    count = event.item_stack.count
  }
  local json_str = helpers.table_to_json(event_data)
  helpers.write_file("events.pipe", json_str .. "\n", true)
end)

-- Periodic production/consumption rate dump (every 120 ticks = 2 seconds)
script.on_nth_tick(120, function(event)
  -- Check if we need to regenerate session ID after load
  check_and_regenerate_session()

  local player_force = game.forces["player"]
  local nauvis = game.surfaces["nauvis"]

  if player_force and nauvis then
    local item_stats = player_force.get_item_production_statistics(nauvis)
    local fluid_stats = player_force.get_fluid_production_statistics(nauvis)

    -- Get player position info and take screenshot
    local player_info = nil
    local screenshot_path = nil
    local player = game.players[1]  -- Get first player
    if player and player.character then
      player_info = {
        position = {x = player.position.x, y = player.position.y},
        surface = player.surface.name,
        health = player.character.health
      }
      
      -- Take screenshot centered on player
      screenshot_path = "screenshots/" .. storage.session_id .. "/tick_" .. event.tick .. ".png"
      game.take_screenshot{
        player = player,
        position = player.position,
        resolution = {x = 1920, y = 1080},
        zoom = 0.5,
        path = screenshot_path,
        show_gui = false,
        show_entity_info = true
      }
    end

    -- Build stats data structure
    local stats_data = {
      type = "stats",
      session_id = storage.session_id,
      cycle = math.floor(event.tick / 120),
      tick = event.tick,
      player = player_info,
      screenshot_path = screenshot_path,
      products_production = {},
      materials_consumption = {}
    }

    -- Collect item production rates (items per minute)
    for item_name, _ in pairs(item_stats.input_counts) do
      local rate = item_stats.get_flow_count{
        name = item_name,
        category = "input",
        precision_index = defines.flow_precision_index.one_minute
      }
      if rate > 0 then
        stats_data.products_production[item_name] = utils.format_number(rate)
      end
    end

    -- Collect item consumption rates (items per minute)
    for item_name, _ in pairs(item_stats.output_counts) do
      local rate = item_stats.get_flow_count{
        name = item_name,
        category = "output",
        precision_index = defines.flow_precision_index.one_minute
      }
      if rate > 0 then
        stats_data.materials_consumption[item_name] = utils.format_number(rate)
      end
    end

    -- Collect fluid production rates (per minute)
    for fluid_name, _ in pairs(fluid_stats.input_counts) do
      local rate = fluid_stats.get_flow_count{
        name = fluid_name,
        category = "input",
        precision_index = defines.flow_precision_index.one_minute
      }
      if rate > 0 then
        stats_data.products_production[fluid_name] = utils.format_number(rate)
      end
    end

    -- Collect fluid consumption rates (per minute)
    for fluid_name, _ in pairs(fluid_stats.output_counts) do
      local rate = fluid_stats.get_flow_count{
        name = fluid_name,
        category = "output",
        precision_index = defines.flow_precision_index.one_minute
      }
      if rate > 0 then
        stats_data.materials_consumption[fluid_name] = utils.format_number(rate)
      end
    end

    -- Convert to JSON and write to named pipe
    local json_str = helpers.table_to_json(stats_data)
    helpers.write_file("events.pipe", json_str .. "\n", true)
  end
end)
