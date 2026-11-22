// Package avahi advertises the web server over mDNS through the system's
// avahi-daemon, so that moipctl can find the node without knowing its IP
// address.
//
// The service is registered over D-Bus for as long as the process runs. A node
// that stops (or crashes) disappears from the network with it, so being
// advertised means being controllable. If avahi-daemon is not running yet, or
// restarts, the registration is retried.
package avahi

import (
	"context"
	"log"
	"time"
)

// Service is what is advertised.
type Service struct {
	Name string   // instance name; avahi picks another on a collision
	Type string   // e.g. "_moip._tcp"
	Port uint16   // TCP port of the HTTP API
	TXT  []string // "key=value" records
}

// EventKind is something that happened to a registration.
type EventKind int

const (
	// Collision: another host already uses the instance name.
	Collision EventKind = iota + 1
	// Failure: avahi gave up on the registration.
	Failure
	// Gone: avahi-daemon left the bus (stopped or restarting).
	Gone
)

// Session is one connection to avahi with one entry group.
type Session interface {
	// Register (re)publishes the service under name.
	Register(name string) error
	// AltName returns a free alternative for a name that collided.
	AltName(name string) (string, error)
	// Events delivers what happens after Register.
	Events() <-chan EventKind
	Close() error
}

// Advertiser keeps a service registered.
type Advertiser struct {
	Service Service
	Dial    func() (Session, error)
	// RetryMin and RetryMax bound the wait before reconnecting.
	RetryMin, RetryMax time.Duration
}

// New returns an Advertiser that talks to the system avahi-daemon.
func New(svc Service) *Advertiser {
	return &Advertiser{Service: svc, Dial: func() (Session, error) { return dialSession(svc) }, RetryMin: 2 * time.Second, RetryMax: 30 * time.Second}
}

// Run keeps the service registered until ctx is done, then withdraws it.
func (a *Advertiser) Run(ctx context.Context) {
	wait := a.RetryMin
	var lastErr string
	for ctx.Err() == nil {
		err := a.once(ctx)
		if ctx.Err() != nil {
			return
		}
		if msg := err.Error(); msg != lastErr {
			log.Printf("avahi: %v (retrying)", err)
			lastErr = msg
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		wait = min(wait*2, a.RetryMax)
	}
}

type errString string

func (e errString) Error() string { return string(e) }

// once registers the service and watches it until something requires a new
// connection (it returns the reason) or ctx is done (it returns nil).
func (a *Advertiser) once(ctx context.Context) error {
	s, err := a.Dial()
	if err != nil {
		return err
	}
	defer s.Close()

	name := a.Service.Name
	if err := s.Register(name); err != nil {
		return err
	}
	log.Printf("avahi: advertising %s as %q on port %d", a.Service.Type, name, a.Service.Port)
	for {
		select {
		case <-ctx.Done():
			return nil
		case ev := <-s.Events():
			switch ev {
			case Collision:
				alt, err := s.AltName(name)
				if err != nil {
					return err
				}
				log.Printf("avahi: name %q is taken, using %q", name, alt)
				name = alt
				if err := s.Register(name); err != nil {
					return err
				}
			case Gone:
				return errString("avahi-daemon went away")
			default:
				return errString("avahi could not register the service")
			}
		}
	}
}
