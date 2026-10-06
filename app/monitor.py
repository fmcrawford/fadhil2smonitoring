                        RUNTIME_STATE["last_error"] = f"{type(exc).__name__}: {exc}"

                self.stop_event.wait(settings.check_interval)
        finally:
            with STATE_LOCK:
                RUNTIME_STATE["running"] = False

    def poll_once(self):
        check_iso = _iso_now()

        with STATE_LOCK:
            RUNTIME_STATE["last_check"] = check_iso
            previous_runtime = list(RUNTIME_STATE["members"])

        dashboard_by_group = {
            group: [m for m in previous_runtime if m["group"] == group]
            for group in GROUP_NAMES
        }

        loop_errors = []
        successful_groups = []

        for index, event in enumerate(EVENTS):
            group = event["group"]
            has_cached_data = bool(dashboard_by_group[group])
            result = self._fetch_group(event)

            if result["kind"] == "success":
                parsed = parse_api_data(result["data"], group, event["buy_url"])
                dashboard_by_group[group] = parsed
                successful_groups.append(group)
                success_iso = result.get("attempt_iso") or check_iso
                self._set_group_success(group, success_iso)

                for item in parsed:
                    uid = item["id"]
                    previous = self.prev_state.get(uid)
                    if previous is not None:
                        try:
                            old_stock = int(previous.get("stock", 0))
                        except Exception:
                            old_stock = 0

                        if old_stock <= 0 and item["stock"] > 0:
                            log.warning(
                                "RESTOCK %s | %s | %s | %s | %s -> %s",
                                item["group"],
                                item["name"],
                                item["session"],
                                item["track"],
                                old_stock,
                                item["stock"],
                            )
                            add_restock_log(item, old_stock, item["stock"])
                            broadcast_restock(item, old_stock)

                    upsert_event_state(item)
                    self.prev_state[uid] = {
                        "uid": uid,
                        "group_name": item["group"],
                        "member_name": item["name"],
                        "session_name": item["session"],
                        "track_name": item["track"],
                        "stock": item["stock"],
                        "updated_at": success_iso,
                    }

                log.info("%s live: %s slot", group, len(parsed))
            else:
                self._set_group_failure(group, result, has_cached_data)
                if result["kind"] != "cooldown":
                    loop_errors.append(f"{group}: {result.get('error', 'fetch gagal')}")
                    log.warning(
                        "%s gagal; status=%s; retry=%ss",
                        group,
                        result.get("status_code"),
                        result.get("cooldown", 0),
                    )

            # Do not burst both endpoints at the exact same instant.
            if index < len(EVENTS) - 1 and not self.stop_event.is_set():
                self.stop_event.wait(1.5)

        combined = []
        for group in GROUP_NAMES:
            combined.extend(dashboard_by_group[group])

        with STATE_LOCK:
            RUNTIME_STATE["members"] = combined

            last_successes = [
                meta.get("last_success")
                for meta in RUNTIME_STATE["groups"].values()
                if meta.get("last_success")
            ]
            RUNTIME_STATE["last_success"] = max(last_successes) if last_successes else None

            if loop_errors:
                RUNTIME_STATE["last_error"] = " | ".join(loop_errors)
            else:
                # If a group is still in cooldown, show a short summary rather
                # than dumping the Cloudflare HTML challenge into the UI.
                waiting = []
                for group, meta in RUNTIME_STATE["groups"].items():
                    if meta.get("status") in ("cooldown", "error") and meta.get("error"):
                        waiting.append(f"{group}: {meta['error']}")
                RUNTIME_STATE["last_error"] = " | ".join(waiting) if waiting else None

        log.info(
            "Polling selesai. live=%s dashboard=%s slot",
            successful_groups,
            len(combined),
        )

    def maybe_send_scheduled_report(self):
        now = _jakarta_now()
        if now.hour not in (8, 12, 20):
            return
        if now.minute >= 5:
            return

        schedule_key = f"{now.date().isoformat()}-{now.hour}"
        if self.last_schedule_key == schedule_key:
            return

        state = snapshot()
        if not state["members"]:
            return
