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

const audioStatsPrefix = "DOUBLETAKE_AUDIO_STATS "

// This protocol contains only fixed names and numeric transport measurements.
// It must never include media, endpoint identifiers, source properties or errors.
// CapturedFrames counts complete encoded codec frames read from local capture.
// PacketsSent includes media-loop FEC, but excludes receiver-requested retransmits.
// StartupDropped includes pre-roll and initial stale frames; StaleDropped also
// includes those initial stale frames. Neither sent count proves audible playout.
type audioStatsReport struct {
	Version          int     `json:"version"`
	Codec            string  `json:"codec"`
	WindowMS         float64 `json:"window_ms"`
	CapturedFrames   int64   `json:"captured_frames"`
	CapturedFPS      float64 `json:"captured_fps"`
	SentFrames       int64   `json:"sent_frames"`
	SentFPS          float64 `json:"sent_fps"`
	PacketsSent      int64   `json:"packets_sent"`
	StaleDropped     int64   `json:"stale_dropped"`
	StartupDropped   int64   `json:"startup_dropped"`
	ClockRebases     int64   `json:"clock_rebases"`
	SourceSamples    int64   `json:"source_samples"`
	CaptureAgeMeanMS float64 `json:"capture_age_mean_ms"`
	CaptureAgeMaxMS  float64 `json:"capture_age_max_ms"`
	PlayoutLeadMS    float64 `json:"playout_lead_ms"`
}

type audioStats struct {
	mu      sync.Mutex
	codec   string
	lead    time.Duration
	started time.Time
	window  audioStatsReport
	ageSum  time.Duration
	ageMax  time.Duration
}

func newAudioStats(codec byte, lead time.Duration, now time.Time) *audioStats {
	name := "unknown"
	switch codec {
	case 2:
		name = "alac"
	case 8:
		name = "aac_eld"
	}
	return &audioStats{codec: name, lead: max(0, lead), started: now}
}

func (s *audioStats) captured(pts, observed time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.window.CapturedFrames++
	if pts.IsZero() {
		return
	}
	age := observed.Sub(pts)
	if age < 0 || age > 30*time.Second {
		return
	}
	s.window.SourceSamples++
	s.ageSum += age
	s.ageMax = max(s.ageMax, age)
}

func (s *audioStats) dropped(startup, stale bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if startup {
		s.window.StartupDropped++
	}
	if stale {
		s.window.StaleDropped++
	}
}

func (s *audioStats) sentPacket() {
	s.mu.Lock()
	s.window.PacketsSent++
	s.mu.Unlock()
}

func (s *audioStats) sentFrame() {
	s.mu.Lock()
	s.window.SentFrames++
	s.mu.Unlock()
}

func (s *audioStats) rebased() {
	s.mu.Lock()
	s.window.ClockRebases++
	s.mu.Unlock()
}

func audioStatsMilliseconds(value time.Duration) float64 {
	return math.Round(float64(value)/float64(time.Millisecond)*100) / 100
}

func (s *audioStats) snapshot(now time.Time) audioStatsReport {
	s.mu.Lock()
	defer s.mu.Unlock()
	elapsed := now.Sub(s.started)
	report := s.window
	report.Version, report.Codec = 1, s.codec
	report.WindowMS = audioStatsMilliseconds(max(0, elapsed))
	report.PlayoutLeadMS = audioStatsMilliseconds(s.lead)
	if elapsed > 0 {
		report.CapturedFPS = math.Round(float64(report.CapturedFrames)/elapsed.Seconds()*100) / 100
		report.SentFPS = math.Round(float64(report.SentFrames)/elapsed.Seconds()*100) / 100
	}
	if report.SourceSamples > 0 {
		report.CaptureAgeMeanMS = audioStatsMilliseconds(s.ageSum / time.Duration(report.SourceSamples))
	}
	report.CaptureAgeMaxMS = audioStatsMilliseconds(s.ageMax)
	s.started = now
	s.window, s.ageSum, s.ageMax = audioStatsReport{}, 0, 0
	return report
}

func writeAudioStats(writer io.Writer, report audioStatsReport) {
	encoded, err := json.Marshal(report)
	if err == nil {
		_, _ = fmt.Fprintf(writer, "%s%s\n", audioStatsPrefix, encoded)
	}
}

func startAudioStats(ctx context.Context, codec byte, lead time.Duration) (*audioStats, func()) {
	stats := newAudioStats(codec, lead, time.Now())
	reportCtx, cancel := context.WithCancel(ctx)
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-reportCtx.Done():
				return
			case <-ticker.C:
				// snapshot unlocks before serialization/output. A slow consumer can
				// delay telemetry, but cannot hold the audio media loop's mutex.
				writeAudioStats(os.Stdout, stats.snapshot(time.Now()))
			}
		}
	}()
	return stats, cancel
}
