IMAGE := harbor.hahomelabs.com/vicegerent/agentgateway
# <upstream-version>-rev<N>, same scheme as hermes-agent: the base tracks the
# AGW_VERSION it was built from and -rev<N> increments on every rebuild that
# changes what the image contains (patches/, Dockerfile, builder base), resetting
# to -rev1 when AGW_VERSION bumps. Renovate reads it under `versioning: loose`.
AGW_VERSION ?= v1.3.1
TAG         ?= $(AGW_VERSION)-rev1
REF         := $(IMAGE):$(TAG)

DOCKER    ?= docker
# Set PLATFORM (e.g. linux/arm64) to cross-build; empty = native.
PLATFORM  ?=
PLAT_FLAG := $(if $(PLATFORM),--platform $(PLATFORM),)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: image
image: ## Build the container image ($(REF))
	$(DOCKER) build $(PLAT_FLAG) --build-arg AGW_VERSION=$(AGW_VERSION) -t $(REF) .

.PHONY: push
push: ## Push the image ($(REF)) to Harbor
	$(DOCKER) push $(REF)

.PHONY: release
release: image push ## Build image, push (override AGW_VERSION=... / TAG=...)
