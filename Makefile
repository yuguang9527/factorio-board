# Load environment variables from .env file if it exists
ifneq (,$(wildcard .env))
    include .env
    export
endif

.PHONY: build-rust-client run-rust-client run-rust-client-debug setup-rust-client read-pipe

# Generate Cargo.toml from template with environment variables
setup-rust-client:
	@if [ -z "$$WANDB_SDK_PATH" ]; then \
		echo "Error: WANDB_SDK_PATH not set. Please configure .env file."; \
		exit 1; \
	fi
	@envsubst < rust_client/Cargo.toml.template > rust_client/Cargo.toml
	@echo "Generated rust_client/Cargo.toml with WANDB_SDK_PATH=$$WANDB_SDK_PATH"

# Build the Rust client
build-rust-client: setup-rust-client
	cd rust_client && cargo build --release

# Run the Rust client with W&B tracking
run-rust-client:
	cd rust_client && WANDB_ENTITY=wandb cargo run

# Run the Rust client with debug logging
run-rust-client-debug:
	cd rust_client && RUST_LOG=debug cargo run

# Read from Factorio named pipe and print to screen
read-pipe:
	@if [ -z "$$FACTORIO_PIPE_PATH" ]; then \
		echo "Error: FACTORIO_PIPE_PATH not set. Please configure .env file."; \
		exit 1; \
	fi
	@echo "Reading from pipe: $$FACTORIO_PIPE_PATH"
	@echo "Press Ctrl+C to stop..."
	@while true; do \
		cat "$$FACTORIO_PIPE_PATH" 2>/dev/null || sleep 1; \
	done
