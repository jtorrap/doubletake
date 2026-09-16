package airplay

import (
	"bytes"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestAudioStatsDistinguishesCaptureFromLateDropsAndDelivery(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newAudioStats(8, 85*time.Millisecond, start)
	for frame := 0; frame < 500; frame++ {
		now := start.Add(time.Duration(frame+1) * 10 * time.Millisecond)
		age := 20 * time.Millisecond
		if frame >= 100 {
			age = 120 * time.Millisecond
		}
		stats.captured(now.Add(-age), now)
		if frame < 20 {
			stats.dropped(true, false)
		} else if frame < 100 {
			stats.sentPacket()
			stats.sentFrame()
		} else {
			stats.dropped(false, true)
		}
	}
	stats.rebased()
	report := stats.snapshot(start.Add(5 * time.Second))
	if report.CapturedFrames != 500 || report.CapturedFPS != 100 || report.SentFrames != 80 || report.SentFPS != 16 || report.PacketsSent != 80 {
		t.Fatalf("capture/delivery = %+v", report)
	}
	if report.StaleDropped != 400 || report.StartupDropped != 20 || report.ClockRebases != 1 {
		t.Fatalf("drop/reset accounting = %+v", report)
	}
	if report.SourceSamples != 500 || report.CaptureAgeMeanMS != 100 || report.CaptureAgeMaxMS != 120 || report.PlayoutLeadMS != 85 {
		t.Fatalf("source timing = %+v", report)
	}
}

func TestAudioStatsUnknownPTSDoesNotLookLikeFreshAudio(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newAudioStats(2, time.Second, start)
	for _, pts := range []time.Time{time.Time{}, start.Add(time.Millisecond), start.Add(-31 * time.Second)} {
		stats.captured(pts, start)
	}
	stats.captured(start.Add(-10*time.Millisecond), start)
	stats.dropped(true, true)
	report := stats.snapshot(start.Add(time.Second))
	if report.CapturedFrames != 4 || report.SourceSamples != 1 || report.CaptureAgeMeanMS != 10 || report.CaptureAgeMaxMS != 10 ||
		report.StaleDropped != 1 || report.StartupDropped != 1 {
		t.Fatalf("invalid source timing/drop overlap = %+v", report)
	}
}

func TestAudioStatsIdleWindowResetsAndUsesObservedDuration(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newAudioStats(2, time.Second, start)
	stats.captured(start, start.Add(time.Millisecond))
	stats.dropped(true, true)
	stats.sentPacket()
	stats.sentFrame()
	stats.rebased()
	first := stats.snapshot(start.Add(5 * time.Second))
	if first.CapturedFrames != 1 {
		t.Fatalf("first = %+v", first)
	}
	idle := stats.snapshot(start.Add(12 * time.Second))
	if idle != (audioStatsReport{Version: 1, Codec: "alac", WindowMS: 7000, PlayoutLeadMS: 1000}) {
		t.Fatalf("idle window retained counters or scheduled duration: %+v", idle)
	}
	stats.captured(start, start)
	zero := stats.snapshot(start.Add(12 * time.Second))
	if zero.WindowMS != 0 || zero.CapturedFPS != 0 || zero.SentFPS != 0 {
		t.Fatalf("zero interval is not finite: %+v", zero)
	}
}

func TestAudioStatsProtocolIsFixedAndContainsNoSourceData(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newAudioStats(255, time.Second, start)
	var output bytes.Buffer
	writeAudioStats(&output, stats.snapshot(start.Add(time.Second)))
	line := output.String()
	if !strings.HasPrefix(line, audioStatsPrefix) || strings.Count(line, "\n") != 1 || len(line) > 640 {
		t.Fatalf("unexpected protocol line %q", line)
	}
	var decoded map[string]any
	if err := json.Unmarshal([]byte(strings.TrimPrefix(line, audioStatsPrefix)), &decoded); err != nil {
		t.Fatal(err)
	}
	fields := []string{"version", "codec", "window_ms", "captured_frames", "captured_fps", "sent_frames", "sent_fps", "packets_sent",
		"stale_dropped", "startup_dropped", "clock_rebases", "source_samples", "capture_age_mean_ms", "capture_age_max_ms", "playout_lead_ms"}
	if len(decoded) != len(fields) {
		t.Fatalf("unexpected field count: %v", decoded)
	}
	for _, key := range fields {
		value, ok := decoded[key]
		if !ok {
			t.Fatalf("missing field %q", key)
		}
		if key == "codec" {
			if value != "unknown" {
				t.Fatalf("unknown codec not allowlisted: %v", value)
			}
		} else if _, ok := value.(float64); !ok {
			t.Fatalf("field %q contains nonnumeric data", key)
		}
	}
}

func TestAudioStatsConcurrentSnapshotsLoseNoCounters(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newAudioStats(2, time.Second, start)
	var workers sync.WaitGroup
	workers.Add(2)
	for range 2 {
		go func() {
			defer workers.Done()
			for range 1000 {
				stats.captured(start, start.Add(time.Millisecond))
				stats.dropped(true, true)
				stats.sentPacket()
				stats.sentFrame()
				stats.rebased()
			}
		}()
	}
	var captured, stale, startup, packets, sent, rebased int64
	add := func(report audioStatsReport) {
		captured += report.CapturedFrames
		stale += report.StaleDropped
		startup += report.StartupDropped
		packets += report.PacketsSent
		sent += report.SentFrames
		rebased += report.ClockRebases
	}
	for n := range 100 {
		add(stats.snapshot(start.Add(time.Duration(n+1) * time.Second)))
	}
	workers.Wait()
	add(stats.snapshot(start.Add(101 * time.Second)))
	if captured != 2000 || stale != 2000 || startup != 2000 || packets != 2000 || sent != 2000 || rebased != 2000 {
		t.Fatalf("lost concurrent counters: %d %d %d %d %d %d", captured, stale, startup, packets, sent, rebased)
	}
}
