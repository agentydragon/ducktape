// Package api: MachineService RPCs for talosctl compatibility.
//
// Implements the MachineService surface needed for talosctl to connect
// and query kubespand. All unimplemented RPCs return Unimplemented.
//
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_server.go
package api

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/hashicorp/go-multierror"
	"github.com/nberlee/go-netstat/netstat"
	"github.com/prometheus/procfs"
	"github.com/siderolabs/gen/xslices"
	"github.com/siderolabs/go-kmsg"
	pointer "github.com/siderolabs/go-pointer"
	"github.com/siderolabs/talos/pkg/machinery/api/common"
	"github.com/siderolabs/talos/pkg/machinery/api/machine"
	"github.com/siderolabs/talos/pkg/machinery/version"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

// MachineServer implements a subset of Talos MachineService RPCs.
// All other RPCs inherit UnimplementedMachineServiceServer (returns Unimplemented).
type MachineServer struct {
	machine.UnimplementedMachineServiceServer
}

func (s *MachineServer) Version(_ context.Context, _ *emptypb.Empty) (*machine.VersionResponse, error) {
	return &machine.VersionResponse{
		Messages: []*machine.Version{
			{
				Version: version.NewVersion(),
			},
		},
	}, nil
}

// Memory returns /proc/meminfo data, matching the Talos machined implementation.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_server.go
func (s *MachineServer) Memory(_ context.Context, _ *emptypb.Empty) (*machine.MemoryResponse, error) {
	proc, err := procfs.NewDefaultFS()
	if err != nil {
		return nil, err
	}

	info, err := proc.Meminfo()
	if err != nil {
		return nil, err
	}

	meminfo := &machine.MemInfo{
		Memtotal:          pointer.SafeDeref(info.MemTotal),
		Memfree:           pointer.SafeDeref(info.MemFree),
		Memavailable:      pointer.SafeDeref(info.MemAvailable),
		Buffers:           pointer.SafeDeref(info.Buffers),
		Cached:            pointer.SafeDeref(info.Cached),
		Swapcached:        pointer.SafeDeref(info.SwapCached),
		Active:            pointer.SafeDeref(info.Active),
		Inactive:          pointer.SafeDeref(info.Inactive),
		Activeanon:        pointer.SafeDeref(info.ActiveAnon),
		Inactiveanon:      pointer.SafeDeref(info.InactiveAnon),
		Activefile:        pointer.SafeDeref(info.ActiveFile),
		Inactivefile:      pointer.SafeDeref(info.InactiveFile),
		Unevictable:       pointer.SafeDeref(info.Unevictable),
		Mlocked:           pointer.SafeDeref(info.Mlocked),
		Swaptotal:         pointer.SafeDeref(info.SwapTotal),
		Swapfree:          pointer.SafeDeref(info.SwapFree),
		Dirty:             pointer.SafeDeref(info.Dirty),
		Writeback:         pointer.SafeDeref(info.Writeback),
		Anonpages:         pointer.SafeDeref(info.AnonPages),
		Mapped:            pointer.SafeDeref(info.Mapped),
		Shmem:             pointer.SafeDeref(info.Shmem),
		Slab:              pointer.SafeDeref(info.Slab),
		Sreclaimable:      pointer.SafeDeref(info.SReclaimable),
		Sunreclaim:        pointer.SafeDeref(info.SUnreclaim),
		Kernelstack:       pointer.SafeDeref(info.KernelStack),
		Pagetables:        pointer.SafeDeref(info.PageTables),
		Nfsunstable:       pointer.SafeDeref(info.NFSUnstable),
		Bounce:            pointer.SafeDeref(info.Bounce),
		Writebacktmp:      pointer.SafeDeref(info.WritebackTmp),
		Commitlimit:       pointer.SafeDeref(info.CommitLimit),
		Committedas:       pointer.SafeDeref(info.CommittedAS),
		Vmalloctotal:      pointer.SafeDeref(info.VmallocTotal),
		Vmallocused:       pointer.SafeDeref(info.VmallocUsed),
		Vmallocchunk:      pointer.SafeDeref(info.VmallocChunk),
		Hardwarecorrupted: pointer.SafeDeref(info.HardwareCorrupted),
		Anonhugepages:     pointer.SafeDeref(info.AnonHugePages),
		Shmemhugepages:    pointer.SafeDeref(info.ShmemHugePages),
		Shmempmdmapped:    pointer.SafeDeref(info.ShmemPmdMapped),
		Cmatotal:          pointer.SafeDeref(info.CmaTotal),
		Cmafree:           pointer.SafeDeref(info.CmaFree),
		Hugepagestotal:    pointer.SafeDeref(info.HugePagesTotal),
		Hugepagesfree:     pointer.SafeDeref(info.HugePagesFree),
		Hugepagesrsvd:     pointer.SafeDeref(info.HugePagesRsvd),
		Hugepagessurp:     pointer.SafeDeref(info.HugePagesSurp),
		Hugepagesize:      pointer.SafeDeref(info.Hugepagesize),
		Directmap4K:       pointer.SafeDeref(info.DirectMap4k),
		Directmap2M:       pointer.SafeDeref(info.DirectMap2M),
		Directmap1G:       pointer.SafeDeref(info.DirectMap1G),
	}

	return &machine.MemoryResponse{
		Messages: []*machine.Memory{
			{
				Meminfo: meminfo,
			},
		},
	}, nil
}

// Mounts returns /proc/mounts data with statfs info, matching the Talos machined implementation.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_server.go
func (s *MachineServer) Mounts(_ context.Context, _ *emptypb.Empty) (reply *machine.MountsResponse, err error) {
	file, err := os.Open("/proc/mounts")
	if err != nil {
		return nil, err
	}
	//nolint:errcheck
	defer file.Close()

	var (
		stat     unix.Statfs_t
		multiErr *multierror.Error
	)

	var stats []*machine.MountStat

	scanner := bufio.NewScanner(file)

	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())

		if len(fields) < 2 {
			continue
		}

		filesystem := fields[0]
		mountpoint := fields[1]

		var (
			totalSize  uint64
			totalAvail uint64
		)

		if statInfo, err := os.Stat(mountpoint); err == nil && statInfo.Mode().IsDir() {
			if err := unix.Statfs(mountpoint, &stat); err != nil {
				multiErr = multierror.Append(multiErr, err)
			} else {
				totalSize = uint64(stat.Bsize) * stat.Blocks
				totalAvail = uint64(stat.Bsize) * stat.Bavail
			}
		}

		stat := &machine.MountStat{
			Filesystem: filesystem,
			Size:       totalSize,
			Available:  totalAvail,
			MountedOn:  mountpoint,
		}

		stats = append(stats, stat)
	}

	if err := scanner.Err(); err != nil {
		multiErr = multierror.Append(multiErr, err)
	}

	reply = &machine.MountsResponse{
		Messages: []*machine.Mounts{
			{
				Stats: stats,
			},
		},
	}

	return reply, multiErr.ErrorOrNil()
}

// Dmesg streams kernel log messages from /dev/kmsg.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_server.go
func (s *MachineServer) Dmesg(req *machine.DmesgRequest, srv grpc.ServerStreamingServer[common.Data]) error {
	ctx := srv.Context()

	var options []kmsg.Option

	if req.Follow {
		options = append(options, kmsg.Follow())
	}

	if req.Tail {
		options = append(options, kmsg.FromTail())
	}

	reader, err := kmsg.NewReader(options...)
	if err != nil {
		return fmt.Errorf("error opening /dev/kmsg reader: %w", err)
	}
	defer reader.Close() //nolint:errcheck

	ch := reader.Scan(ctx)

	for {
		select {
		case <-ctx.Done():
			if err = reader.Close(); err != nil {
				return err
			}
		case packet, ok := <-ch:
			if !ok {
				return nil
			}

			if packet.Err != nil {
				err = srv.Send(&common.Data{
					Metadata: &common.Metadata{
						Error: packet.Err.Error(),
					},
				})
			} else {
				msg := packet.Message
				err = srv.Send(&common.Data{
					Bytes: fmt.Appendf(nil, "%s: %7s: [%s]: %s", msg.Facility, msg.Priority, msg.Timestamp.Format(time.RFC3339Nano), msg.Message),
				})
			}

			if err != nil {
				return err
			}
		}
	}
}

// Netstat returns network socket information, matching the Talos machined implementation.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_server.go
func (s *MachineServer) Netstat(ctx context.Context, req *machine.NetstatRequest) (*machine.NetstatResponse, error) {
	if req == nil {
		req = new(machine.NetstatRequest)
	}

	var features netstat.EnableFeatures
	if req.L4Proto != nil {
		features.TCP = req.L4Proto.Tcp
		features.TCP6 = req.L4Proto.Tcp6
		features.UDP = req.L4Proto.Udp
		features.UDP6 = req.L4Proto.Udp6
		features.UDPLite = req.L4Proto.Udplite
		features.UDPLite6 = req.L4Proto.Udplite6
		features.Raw = req.L4Proto.Raw
		features.Raw6 = req.L4Proto.Raw6
	}
	if req.Feature != nil {
		features.PID = req.Feature.Pid
	}

	if req.Netns != nil {
		features.NoHostNetwork = !req.Netns.Hostnetwork
		features.AllNetNs = req.Netns.Allnetns
		features.NetNsName = req.Netns.Netns
	}

	var fn netstat.AcceptFn

	switch req.Filter {
	case machine.NetstatRequest_ALL:
		fn = func(*netstat.SockTabEntry) bool { return true }
	case machine.NetstatRequest_LISTENING:
		fn = func(s *netstat.SockTabEntry) bool {
			return s.RemoteEndpoint.IP.IsUnspecified() && s.RemoteEndpoint.Port == 0
		}
	case machine.NetstatRequest_CONNECTED:
		fn = func(s *netstat.SockTabEntry) bool {
			return !s.RemoteEndpoint.IP.IsUnspecified() && s.RemoteEndpoint.Port != 0
		}
	}

	netstatResp, err := netstat.Netstat(ctx, features, fn)
	if err != nil {
		return nil, err
	}

	records := make([]*machine.ConnectRecord, len(netstatResp))

	for i, entry := range netstatResp {
		records[i] = &machine.ConnectRecord{
			L4Proto:    entry.Transport,
			Localip:    entry.LocalEndpoint.IP.String(),
			Localport:  uint32(entry.LocalEndpoint.Port),
			Remoteip:   entry.RemoteEndpoint.IP.String(),
			Remoteport: uint32(entry.RemoteEndpoint.Port),
			State:      machine.ConnectRecord_State(entry.State),
			Txqueue:    entry.TxQueue,
			Rxqueue:    entry.RxQueue,
			Tr:         machine.ConnectRecord_TimerActive(entry.Tr),
			Timerwhen:  entry.TimerWhen,
			Retrnsmt:   entry.Retrnsmt,
			Uid:        entry.UID,
			Timeout:    entry.Timeout,
			Inode:      entry.Inode,
			Ref:        entry.Ref,
			Pointer:    entry.Pointer,
			Process:    &machine.ConnectRecord_Process{},
			Netns:      entry.NetNS,
		}
		if entry.Process != nil {
			records[i].Process = &machine.ConnectRecord_Process{
				Pid:  uint32(entry.Process.Pid),
				Name: entry.Process.Name,
			}
		}
	}

	reply := &machine.NetstatResponse{
		Messages: []*machine.Netstat{
			{
				Connectrecord: records,
			},
		},
	}

	return reply, err
}

// Hostname implements the machine.MachineServer interface.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_monitoring.go
func (s *MachineServer) Hostname(_ context.Context, _ *emptypb.Empty) (*machine.HostnameResponse, error) {
	hostname, err := os.Hostname()
	if err != nil {
		return nil, err
	}

	return &machine.HostnameResponse{
		Messages: []*machine.Hostname{
			{
				Hostname: hostname,
			},
		},
	}, nil
}

// LoadAvg implements the machine.MachineServer interface.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_monitoring.go
func (s *MachineServer) LoadAvg(_ context.Context, _ *emptypb.Empty) (*machine.LoadAvgResponse, error) {
	fs, err := procfs.NewDefaultFS()
	if err != nil {
		return nil, err
	}

	loadAvg, err := fs.LoadAvg()
	if err != nil {
		return nil, err
	}

	return &machine.LoadAvgResponse{
		Messages: []*machine.LoadAvg{
			{
				Load1:  loadAvg.Load1,
				Load5:  loadAvg.Load5,
				Load15: loadAvg.Load15,
			},
		},
	}, nil
}

// SystemStat implements the machine.MachineServer interface.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_monitoring.go
func (s *MachineServer) SystemStat(_ context.Context, _ *emptypb.Empty) (*machine.SystemStatResponse, error) {
	fs, err := procfs.NewDefaultFS()
	if err != nil {
		return nil, err
	}

	stat, err := fs.Stat()
	if err != nil {
		return nil, err
	}

	translateCPUStat := func(in procfs.CPUStat) *machine.CPUStat {
		return &machine.CPUStat{
			User:      in.User,
			Nice:      in.Nice,
			System:    in.System,
			Idle:      in.Idle,
			Iowait:    in.Iowait,
			Irq:       in.IRQ,
			SoftIrq:   in.SoftIRQ,
			Steal:     in.Steal,
			Guest:     in.Guest,
			GuestNice: in.GuestNice,
		}
	}

	translateListOfCPUStat := func(in map[int64]procfs.CPUStat) []*machine.CPUStat {
		maxCore := int64(-1)

		for core := range in {
			maxCore = max(maxCore, core)
		}

		slc := make([]*machine.CPUStat, maxCore+1)

		for core, stat := range in {
			slc[core] = translateCPUStat(stat)
		}

		return slc
	}

	translateSoftIRQ := func(in procfs.SoftIRQStat) *machine.SoftIRQStat {
		return &machine.SoftIRQStat{
			Hi:          in.Hi,
			Timer:       in.Timer,
			NetTx:       in.NetTx,
			NetRx:       in.NetRx,
			Block:       in.Block,
			BlockIoPoll: in.BlockIoPoll,
			Tasklet:     in.Tasklet,
			Sched:       in.Sched,
			Hrtimer:     in.Hrtimer,
			Rcu:         in.Rcu,
		}
	}

	return &machine.SystemStatResponse{
		Messages: []*machine.SystemStat{
			{
				BootTime:        stat.BootTime,
				CpuTotal:        translateCPUStat(stat.CPUTotal),
				Cpu:             translateListOfCPUStat(stat.CPU),
				IrqTotal:        stat.IRQTotal,
				Irq:             stat.IRQ,
				ContextSwitches: stat.ContextSwitches,
				ProcessCreated:  stat.ProcessCreated,
				ProcessRunning:  stat.ProcessesRunning,
				ProcessBlocked:  stat.ProcessesBlocked,
				SoftIrqTotal:    stat.SoftIRQTotal,
				SoftIrq:         translateSoftIRQ(stat.SoftIRQ),
			},
		},
	}, nil
}

// CPUInfo implements the machine.MachineServer interface.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_monitoring.go
func (s *MachineServer) CPUInfo(_ context.Context, _ *emptypb.Empty) (*machine.CPUInfoResponse, error) {
	fs, err := procfs.NewDefaultFS()
	if err != nil {
		return nil, err
	}

	info, err := fs.CPUInfo()
	if err != nil {
		return nil, err
	}

	translateCPUInfo := func(in procfs.CPUInfo) *machine.CPUInfo {
		return &machine.CPUInfo{
			Processor:       uint32(in.Processor),
			VendorId:        in.VendorID,
			CpuFamily:       in.CPUFamily,
			Model:           in.Model,
			ModelName:       in.ModelName,
			Stepping:        in.Stepping,
			Microcode:       in.Microcode,
			CpuMhz:          in.CPUMHz,
			CacheSize:       in.CacheSize,
			PhysicalId:      in.PhysicalID,
			Siblings:        uint32(in.Siblings),
			CoreId:          in.CoreID,
			ApicId:          in.APICID,
			InitialApicId:   in.InitialAPICID,
			Fpu:             in.FPU,
			FpuException:    in.FPUException,
			CpuIdLevel:      uint32(in.CPUIDLevel),
			Wp:              in.WP,
			Flags:           in.Flags,
			Bugs:            in.Bugs,
			BogoMips:        in.BogoMips,
			ClFlushSize:     uint32(in.CLFlushSize),
			CacheAlignment:  uint32(in.CacheAlignment),
			AddressSizes:    in.AddressSizes,
			PowerManagement: in.PowerManagement,
		}
	}

	return &machine.CPUInfoResponse{
		Messages: []*machine.CPUsInfo{
			{
				CpuInfo: xslices.Map(info, translateCPUInfo),
			},
		},
	}, nil
}

// DiskUsage implements the machine.MachineServer interface.
// Ref: internal/app/machined/internal/server/v1alpha1/v1alpha1_server.go
func (s *MachineServer) DiskUsage(req *machine.DiskUsageRequest, srv machine.MachineService_DiskUsageServer) error {
	if req == nil {
		req = new(machine.DiskUsageRequest)
	}

	for _, path := range req.Paths {
		if !strings.HasPrefix(path, "/") {
			path = "/" + path
		}

		path = strings.TrimSuffix(path, "/")
		if path == "" {
			path = "/"
		}

		if _, err := os.Stat(path); os.IsNotExist(err) {
			if err := srv.Send(&machine.DiskUsageInfo{
				Name:         path,
				RelativeName: path,
				Error:        err.Error(),
			}); err != nil {
				return err
			}
			continue
		}

		folders := map[string]*machine.DiskUsageInfo{}
		rootDepth := strings.Count(path, "/")

		err := filepath.WalkDir(path, func(fullPath string, d os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return srv.Send(&machine.DiskUsageInfo{
					Name:         fullPath,
					RelativeName: relPath(path, fullPath),
					Error:        walkErr.Error(),
				})
			}

			info, err := d.Info()
			if err != nil {
				return srv.Send(&machine.DiskUsageInfo{
					Name:         fullPath,
					RelativeName: relPath(path, fullPath),
					Error:        err.Error(),
				})
			}

			currentDepth := int32(strings.Count(fullPath, "/")) - int32(rootDepth)
			size := max(info.Size(), 0)

			// /proc/kcore reports a misleading size.
			if fullPath == "/proc/kcore" {
				size = 0
			}

			if d.IsDir() {
				folders[strings.TrimRight(fullPath, "/")] = &machine.DiskUsageInfo{
					Name:         fullPath,
					RelativeName: relPath(path, fullPath),
					Size:         size,
				}
				// Enforce recursion depth.
				if req.RecursionDepth > 0 && currentDepth >= req.RecursionDepth {
					return filepath.SkipDir
				}
				return nil
			}

			// Accumulate size in parent folder.
			parent := strings.TrimRight(filepath.Dir(fullPath), "/")
			if folder, ok := folders[parent]; ok {
				folder.Size += size
			}

			// Skip files unless All is set.
			if !req.All {
				return nil
			}

			// Threshold filter.
			if req.Threshold > 0 && size < req.Threshold {
				return nil
			}
			if req.Threshold < 0 && size > -req.Threshold {
				return nil
			}

			return srv.Send(&machine.DiskUsageInfo{
				Name:         fullPath,
				RelativeName: relPath(path, fullPath),
				Size:         size,
			})
		})
		if err != nil {
			return err
		}

		// Flush accumulated folder sizes (deepest first via sorted keys).
		// Send root folder last.
		rootKey := strings.TrimRight(path, "/")
		for key, folder := range folders {
			if key == rootKey {
				continue
			}
			// Accumulate into parent.
			parent := strings.TrimRight(filepath.Dir(key), "/")
			if pf, ok := folders[parent]; ok {
				pf.Size += folder.Size
			}

			currentDepth := int32(strings.Count(key, "/")) - int32(rootDepth)
			skip := req.RecursionDepth > 0 && currentDepth >= req.RecursionDepth
			skip = skip || (req.Threshold > 0 && folder.Size < req.Threshold)
			skip = skip || (req.Threshold < 0 && folder.Size > -req.Threshold)
			if !skip {
				if err := srv.Send(folder); err != nil {
					return err
				}
			}
		}

		// Send root folder.
		if folder, ok := folders[rootKey]; ok {
			if err := srv.Send(folder); err != nil {
				return err
			}
		}
	}

	return nil
}

func relPath(base, full string) string {
	rel, err := filepath.Rel(base, full)
	if err != nil {
		return full
	}
	return rel
}

// TODO: implement these additional Talos MachineService RPCs:
// - Processes() — reads /proc for process list
// - Containers() — lists running containers
// - Logs() — streaming RPC for container logs

// RegisterMachineService registers the MachineService on a gRPC server.
func RegisterMachineService(srv *grpc.Server) {
	machine.RegisterMachineServiceServer(srv, &MachineServer{})
}
