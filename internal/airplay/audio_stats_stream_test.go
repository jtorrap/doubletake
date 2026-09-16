package airplay

import (
	"context"
	"errors"
	"io"
	"net"
	"testing"
	"time"
)

type diagnosticPCMFrames struct {
	times []time.Time
}

func (r *diagnosticPCMFrames) ReadPCMFrame(dst []byte) (time.Time, error) {
	if len(r.times) == 0 {
		return time.Time{}, io.EOF
	}
	clear(dst)
	pts := r.times[0]
	r.times = r.times[1:]
	return pts, nil
}

func TestStreamAudioStatsCountsActualStartupAndSteadyDrops(t *testing.T) {
	ctrl, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ctrl.Close()
	peer, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer peer.Close()
	firstVideo := make(chan struct{})
	close(firstVideo)
	session := &MirrorSession{firstFrameSent: firstVideo, timingProtocol: timingProtocolNTP}
	media := &recordingPacketConn{}
	stream := &AudioStream{conn: media, ctrlConn: ctrl, ctrlAddr: peer.LocalAddr().(*net.UDPAddr),
		remoteAddr: peer.LocalAddr().(*net.UDPAddr), spf: 352, ct: byte(AudioCodecALAC), latencySamples: 22050}
	now := time.Now()
	capture := &AudioCapture{pcmFrames: &diagnosticPCMFrames{times: []time.Time{
		now.Add(-2 * time.Second), now.Add(-50 * time.Millisecond),
		now.Add(-2 * time.Second), now.Add(-80 * time.Millisecond),
	}}, waitCh: make(chan struct{}), codec: AudioCodecALAC}
	stats := newAudioStats(stream.ct, audioLatencyDuration(stream.latencySamples), now)
	err = session.streamAudio(context.Background(), capture, stream, stats)
	if !errors.Is(err, io.EOF) {
		t.Fatalf("stream result = %v, want EOF", err)
	}
	report := stats.snapshot(now.Add(time.Second))
	if report.CapturedFrames != 4 || report.SentFrames != 2 || report.PacketsSent != 2 || len(media.packets) != 2 ||
		report.StartupDropped != 1 || report.StaleDropped != 2 || report.ClockRebases != 1 || report.SourceSamples != 4 {
		t.Fatalf("actual stream accounting = %+v, packets=%d", report, len(media.packets))
	}
}
