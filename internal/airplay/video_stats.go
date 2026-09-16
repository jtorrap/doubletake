package airplay

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"sync"
	"time"
)

const videoStatsPrefix = "DOUBLETAKE_VIDEO_STATS "

// videoStatsReport contains only bounded numeric measurements and an allowlisted
// codec name. Never add receiver identifiers, page data, errors, or payloads to
// this stdout protocol: the app may surface it without enabling debug logging.
// Frames count successfully written encoded pictures, not unique source images
// or pictures displayed by the receiver. Source age ends at the local socket
// write; network transit and the receiver's decode/display delay are not known.
type videoStatsReport struct {
	Version         int     `json:"version"`
	Codec           string  `json:"codec"`
	WindowMS        float64 `json:"window_ms"`
	Frames          int64   `json:"frames"`
	SentFPS         float64 `json:"sent_fps"`
	SourceSamples   int64   `json:"source_samples"`
	SourceAgeMeanMS float64 `json:"source_age_mean_ms"`
	SourceAgeMaxMS  float64 `json:"source_age_max_ms"`
	WriteMeanMS     float64 `json:"write_mean_ms"`
	WriteMaxMS      float64 `json:"write_max_ms"`
	LateFrames      int64   `json:"late_frames"`
	PlayoutLeadMS   float64 `json:"playout_lead_ms"`
}

type videoStats struct {
	mu            sync.Mutex
	codec         string
	lead          time.Duration
	started       time.Time
	frames        int64
	sourceSamples int64
	ageSum        time.Duration
	ageMax        time.Duration
	writeSum      time.Duration
	writeMax      time.Duration
	late          int64
}

func newVideoStats(codec string, lead time.Duration, now time.Time) *videoStats {
	name := "h264"
	if codec == "hevc" {
		name = "hevc"
	}
	return &videoStats{codec: name, lead: max(0, lead), started: now}
}

// record is called only after a complete encoded access unit was written. A
// short mutex protects a handful of counters; reporting never blocks this path
// on JSON encoding or stdout. Unknown, future, or implausible PTS values do not
// masquerade as zero-age samples.
func (s *videoStats) record(capturedAt, sentAt time.Time, writeDuration time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.frames++
	if writeDuration >= 0 {
		s.writeSum += writeDuration
		s.writeMax = max(s.writeMax, writeDuration)
	}
	if capturedAt.IsZero() {
		return
	}
	age := sentAt.Sub(capturedAt)
	if age < 0 || age > 30*time.Second {
		return
	}
	s.sourceSamples++
	s.ageSum += age
	s.ageMax = max(s.ageMax, age)
	if age >= s.lead {
		s.late++
	}
}

func statsMilliseconds(value time.Duration) float64 {
	return math.Round(float64(value)/float64(time.Millisecond)*100) / 100
}

func (s *videoStats) snapshot(now time.Time) videoStatsReport {
	s.mu.Lock()
	defer s.mu.Unlock()
	elapsed := now.Sub(s.started)
	report := videoStatsReport{
		Version: 1, Codec: s.codec, WindowMS: statsMilliseconds(max(0, elapsed)),
		Frames: s.frames, SourceSamples: s.sourceSamples,
		SourceAgeMaxMS: statsMilliseconds(s.ageMax), WriteMaxMS: statsMilliseconds(s.writeMax),
		LateFrames: s.late, PlayoutLeadMS: statsMilliseconds(s.lead),
	}
	if elapsed > 0 {
		report.SentFPS = math.Round(float64(s.frames)/elapsed.Seconds()*100) / 100
	}
	if s.frames > 0 {
		report.WriteMeanMS = statsMilliseconds(s.writeSum / time.Duration(s.frames))
	}
	if s.sourceSamples > 0 {
		report.SourceAgeMeanMS = statsMilliseconds(s.ageSum / time.Duration(s.sourceSamples))
	}
	s.started = now
	s.frames, s.sourceSamples, s.ageSum, s.ageMax = 0, 0, 0, 0
	s.writeSum, s.writeMax, s.late = 0, 0, 0
	return report
}

func writeVideoStats(writer io.Writer, report videoStatsReport) {
	// This concrete struct cannot include private caller-supplied string data.
	encoded, err := json.Marshal(report)
	if err == nil {
		_, _ = fmt.Fprintf(writer, "%s%s\n", videoStatsPrefix, encoded)
	}
}

func startVideoStats(ctx context.Context, codec string, lead time.Duration) (*videoStats, func()) {
	stats := newVideoStats(codec, lead, time.Now())
	reportCtx, cancel := context.WithCancel(ctx)
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-reportCtx.Done():
				return
			case <-ticker.C:
				// Use observation time rather than a delayed ticker's scheduled time.
				// Idle windows report zero, making a stalled sender visible too.
				writeVideoStats(os.Stdout, stats.snapshot(time.Now()))
			}
		}
	}()
	return stats, cancel
}
