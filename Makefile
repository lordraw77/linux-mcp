IMAGE   := lordraw/linux-mcp
VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
LATEST  := $(IMAGE):latest
TAG     := $(IMAGE):$(VERSION)

.DEFAULT_GOAL := help

.PHONY: help build tag push release run clean

help:
	@echo ""
	@echo "  linux-ssh-mcp — Docker helpers"
	@echo ""
	@echo "  make build     Build image ($(IMAGE))"
	@echo "  make tag       Tag with current git version ($(VERSION))"
	@echo "  make push      Push :latest and :$(VERSION) to Docker Hub"
	@echo "  make release   build + tag + push"
	@echo "  make run       Run server locally (reads .env from current dir)"
	@echo "  make clean     Remove local image"
	@echo ""

build:
	docker build --platform linux/amd64 -t $(LATEST) .

tag: build
	docker tag $(LATEST) $(TAG)

push:
	docker push $(LATEST)
	docker push $(TAG)

release: build tag push

run:
	docker run --rm -i \
		--env-file .env \
		$(LATEST)

clean:
	docker rmi -f $(LATEST) $(TAG) 2>/dev/null || true
