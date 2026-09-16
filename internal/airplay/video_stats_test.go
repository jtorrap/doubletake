package airplay

import (
	"bytes"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestVideoStatsMeasuresEncodedThroughputAndLocalDeadlines(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newVideoStats("h264", 75*time.Millisecond, start)
	for frame := 0; frame < 150; frame++ {
		sent := start.Add(time.Duration(frame+1) * (time.Second / 30))
		age := 40 * time.Millisecond
		write := 2 * time.Millisecond
		if frame >= 100 {
			age, write = 100*time.Millisecond, 8*time.Millisecond
		}
		stats.record(sent.Add(-age), sent, write)
	}
	report := stats.snapshot(start.Add(5 * time.Second))
	if report.Frames != 150 || report.SentFPS != 30 || report.WindowMS != 5000 {
		t.Fatalf("throughput = %+v", report)
	}
	if report.SourceSamples != 150 || report.SourceAgeMeanMS != 60 || report.SourceAgeMaxMS != 100 || report.LateFrames != 50 {
		t.Fatalf("capture timing = %+v", report)
	}
	if report.WriteMeanMS != 4 || report.WriteMaxMS != 8 || report.PlayoutLeadMS != 75 {
		t.Fatalf("write/playout timing = %+v", report)
	}
}

func TestVideoStatsDoesNotTreatMissingOrInvalidPTSAsFreshFrames(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newVideoStats("hevc", 100*time.Millisecond, start)
	sent := start.Add(time.Second)
	for _, pts := range []time.Time{time.Time{}, sent.Add(time.Millisecond), sent.Add(-31 * time.Second)} {
		stats.record(pts, sent, time.Millisecond)
	}
	stats.record(sent.Add(-100*time.Millisecond), sent, time.Millisecond)
	report := stats.snapshot(start.Add(5 * time.Second))
	if report.Frames != 4 || report.SourceSamples != 1 || report.LateFrames != 1 || report.SourceAgeMeanMS != 100 {
		t.Fatalf("missing timestamps were counted as measurements: %+v", report)
	}
	if report.Codec != "hevc" {
		t.Fatalf("codec = %q", report.Codec)
	}
}

func TestVideoStatsWindowsResetAndIdleSenderReportsZero(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newVideoStats("h264", 75*time.Millisecond, start)
	stats.record(start, start.Add(time.Millisecond), time.Millisecond)
	first := stats.snapshot(start.Add(5 * time.Second))
	if first.Frames != 1 {
		t.Fatalf("first report = %+v", first)
	}
	idle := stats.snapshot(start.Add(11 * time.Second))
	if idle.WindowMS != 6000 || idle.Frames != 0 || idle.SentFPS != 0 || idle.SourceSamples != 0 ||
		idle.SourceAgeMeanMS != 0 || idle.SourceAgeMaxMS != 0 || idle.WriteMeanMS != 0 || idle.WriteMaxMS != 0 || idle.LateFrames != 0 {
		t.Fatalf("idle window retained previous measurements: %+v", idle)
	}
	stats.record(start, start, 0)
	zeroWindow := stats.snapshot(start.Add(11 * time.Second))
	if zeroWindow.SentFPS != 0 || zeroWindow.WindowMS != 0 {
		t.Fatalf("zero-duration report must stay finite: %+v", zeroWindow)
	}
}

func TestVideoStatsProtocolContainsOnlyFixedFieldsAndAllowlistedCodec(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newVideoStats("private-page-or-receiver-name", 75*time.Millisecond, start)
	var output bytes.Buffer
	writeVideoStats(&output, stats.snapshot(start.Add(5*time.Second)))
	line := output.String()
	if !strings.HasPrefix(line, videoStatsPrefix) || strings.Count(line, "\n") != 1 || strings.Contains(line, "private") || len(line) > 512 {
		t.Fatalf("invalid bounded protocol line %q", line)
	}
	var decoded map[string]any
	if err := json.Unmarshal([]byte(strings.TrimPrefix(line, videoStatsPrefix)), &decoded); err != nil {
		t.Fatal(err)
	}
	fields := []string{"version", "codec", "window_ms", "frames", "sent_fps", "source_samples", "source_age_mean_ms", "source_age_max_ms", "write_mean_ms", "write_max_ms", "late_frames", "playout_lead_ms"}
	if len(decoded) != len(fields) {
		t.Fatalf("unexpected field count: %v", decoded)
	}
	for _, field := range fields {
		value, ok := decoded[field]
		if !ok {
			t.Fatalf("missing %s", field)
		}
		if field == "codec" {
			if value != "h264" {
				t.Fatalf("unvalidated codec value %v", value)
			}
		} else if _, ok := value.(float64); !ok {
			t.Fatalf("non-numeric %s = %v", field, value)
		}
	}
}

func TestVideoStatsConcurrentRecordsAndSnapshotsLoseNoFrames(t *testing.T) {
	start := time.Unix(1000, 0)
	stats := newVideoStats("h264", time.Second, start)
	var workers sync.WaitGroup
	workers.Add(2)
	for range 2 {
		go func() {
			defer workers.Done()
			for range 1000 {
				stats.record(start, start.Add(time.Millisecond), time.Millisecond)
			}
		}()
	}
	var total int64
	for n := range 100 {
		total += stats.snapshot(start.Add(time.Duration(n+1) * time.Second)).Frames
	}
	workers.Wait()
	total += stats.snapshot(start.Add(101 * time.Second)).Frames
	if total != 2000 {
		t.Fatalf("lost concurrent records: got %d, want 2000", total)
	}
}
