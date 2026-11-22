package avahi

import (
	"errors"
	"fmt"

	"github.com/godbus/dbus/v5"
)

// The Avahi D-Bus API: https://avahi.org/doxygen/html/ (org.freedesktop.Avahi.*)
const (
	busName     = "org.freedesktop.Avahi"
	ifServer    = "org.freedesktop.Avahi.Server"
	ifGroup     = "org.freedesktop.Avahi.EntryGroup"
	ifUnspec    = int32(-1) // IF_UNSPEC: all interfaces
	protoUnspec = int32(-1) // PROTO_UNSPEC: IPv4 and IPv6

	errCollision = "org.freedesktop.Avahi.CollisionError"

	groupCollision = int32(3) // AVAHI_ENTRY_GROUP_COLLISION
	groupFailure   = int32(4) // AVAHI_ENTRY_GROUP_FAILURE
)

type dbusSession struct {
	svc    Service
	conn   *dbus.Conn
	server dbus.BusObject
	group  dbus.BusObject
	path   dbus.ObjectPath
	sigs   chan *dbus.Signal
	events chan EventKind
	done   chan struct{}
}

// dialSession connects to the system avahi-daemon and creates an entry group
// for svc (type, port and TXT; the instance name is given to Register).
func dialSession(svc Service) (Session, error) {
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		return nil, fmt.Errorf("system bus: %w", err)
	}
	s := &dbusSession{svc: svc, conn: conn, events: make(chan EventKind, 8), done: make(chan struct{})}
	var has bool
	if err := conn.BusObject().Call("org.freedesktop.DBus.NameHasOwner", 0, busName).Store(&has); err != nil || !has {
		conn.Close()
		return nil, errString("avahi-daemon is not running")
	}
	s.server = conn.Object(busName, "/")
	if err := s.server.Call(ifServer+".EntryGroupNew", 0).Store(&s.path); err != nil {
		conn.Close()
		return nil, fmt.Errorf("EntryGroupNew: %w", err)
	}
	s.group = conn.Object(busName, s.path)

	for _, opt := range [][]dbus.MatchOption{
		{dbus.WithMatchObjectPath(s.path), dbus.WithMatchInterface(ifGroup), dbus.WithMatchMember("StateChanged")},
		{dbus.WithMatchSender("org.freedesktop.DBus"), dbus.WithMatchInterface("org.freedesktop.DBus"),
			dbus.WithMatchMember("NameOwnerChanged"), dbus.WithMatchArg(0, busName)},
	} {
		if err := conn.AddMatchSignal(opt...); err != nil {
			conn.Close()
			return nil, fmt.Errorf("AddMatch: %w", err)
		}
	}
	s.sigs = make(chan *dbus.Signal, 16)
	conn.Signal(s.sigs)
	go s.pump()
	return s, nil
}

// pump turns D-Bus signals into Events.
func (s *dbusSession) pump() {
	emit := func(k EventKind) {
		select {
		case s.events <- k:
		default: // the reader is far behind; it will see the next one
		}
	}
	for {
		select {
		case <-s.done:
			return
		case sig, ok := <-s.sigs:
			if !ok {
				return
			}
			switch sig.Name {
			case ifGroup + ".StateChanged":
				if sig.Path != s.path || len(sig.Body) == 0 {
					continue
				}
				switch st, _ := sig.Body[0].(int32); st {
				case groupCollision:
					emit(Collision)
				case groupFailure:
					emit(Failure)
				}
			case "org.freedesktop.DBus.NameOwnerChanged":
				// Body: name, old owner, new owner. An empty new owner means avahi left.
				if len(sig.Body) == 3 {
					if newOwner, _ := sig.Body[2].(string); newOwner == "" {
						emit(Gone)
					}
				}
			}
		}
	}
}

func (s *dbusSession) Register(name string) error {
	if err := s.group.Call(ifGroup+".Reset", 0).Err; err != nil {
		return fmt.Errorf("Reset: %w", err)
	}
	txt := make([][]byte, len(s.svc.TXT))
	for i, t := range s.svc.TXT {
		txt[i] = []byte(t)
	}
	if err := s.group.Call(ifGroup+".AddService", 0,
		ifUnspec, protoUnspec, uint32(0), name, s.svc.Type, "", "", s.svc.Port, txt).Err; err != nil {
		// avahi answers a name that is already used on this host with an
		// error here, not with a state change.
		var de dbus.Error
		if errors.As(err, &de) && de.Name == errCollision {
			return fmt.Errorf("AddService %q: %w", name, ErrCollision)
		}
		return fmt.Errorf("AddService: %w", err)
	}
	if err := s.group.Call(ifGroup+".Commit", 0).Err; err != nil {
		return fmt.Errorf("Commit: %w", err)
	}
	return nil
}

func (s *dbusSession) AltName(name string) (string, error) {
	var alt string
	if err := s.server.Call(ifServer+".GetAlternativeServiceName", 0, name).Store(&alt); err != nil {
		return "", fmt.Errorf("GetAlternativeServiceName: %w", err)
	}
	return alt, nil
}

func (s *dbusSession) Events() <-chan EventKind { return s.events }

func (s *dbusSession) Close() error {
	close(s.done)
	s.group.Call(ifGroup+".Free", 0) // withdraws the service; ignore errors, the bus may be gone
	s.conn.RemoveSignal(s.sigs)
	return s.conn.Close()
}
