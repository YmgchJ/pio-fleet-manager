"""PDG1 sidecars: persist before ACK, independent of legacy log-session merging."""
from pathlib import Path
import struct,json,re,hashlib,math,os,csv,time
HEADER=struct.Struct('<4sIHHII40s32s9fqI')
RECORD=struct.Struct('<IIIHH7f')
FIELDS=['micros32','update_micros32','interval_us','trial','ticks','phase_rad','raw_mA','ma_mA','span_mA','signal','correction_integral_rad','cumulative_correction_rad']
CONFIG=['kappa','omega','a0','a1','b1','center','amplitude','tau','harmonics']
def token_for(seq):
    value=2166136261
    for b in seq.encode(): value=((value^b)*16777619)&0xffffffff
    return value
class DiagnosticReceiver:
    def __init__(self,root):self.root=Path(root)
    def handle(self,data):
        if len(data)<HEADER.size:return None
        magic,token,agent,size,offset,total,seq,build,*extra=HEADER.unpack_from(data)
        try:seq=seq.split(b'\0')[0].decode('ascii');build=build.split(b'\0')[0].decode('ascii')
        except UnicodeError:return None
        payload=data[HEADER.size:];n=len(payload)//RECORD.size
        if (magic!=b'PDG1' or size!=RECORD.size or not re.fullmatch(r'ps[0-9_]+',seq)
            or token!=token_for(seq) or not 1<=agent<=99 or not 1<=total<=1100
            or len(payload)%size or not 1<=n<=8 or offset%8 or offset+n>total
            or n!=min(8,total-offset) or not build.startswith('pdg1-')
            or any(not math.isfinite(x) for x in extra[:9])):return None
        decoded=list(RECORD.iter_unpack(payload))
        if any(any(not math.isfinite(x) for x in r[5:]) for r in decoded):return None
        directory=self.root/seq/f'agent_{agent}';directory.mkdir(parents=True,exist_ok=True)
        meta=dict(schema='PDG1',sequence=seq,agent=agent,build=build,record_size=size,total=total,
                  config=dict(zip(CONFIG,extra[:9])),clock_offset_us=extra[9],clock_rtt_us=extra[10])
        meta_path=directory/'metadata.json'
        if meta_path.exists():
            if json.loads(meta_path.read_text())!=meta:return None
        else:self._write(meta_path,json.dumps(meta,indent=2).encode())
        chunk=directory/f'{offset:06d}.bin'
        if chunk.exists():
            if chunk.read_bytes()!=payload:return None
        else:self._write(chunk,payload)
        chunks=[directory/f'{i:06d}.bin' for i in range(0,total,8)]
        if all(p.exists() for p in chunks):
            raw=b''.join(p.read_bytes() for p in chunks)
            if len(raw)!=total*size:return None
            self._write(directory/'records.bin',raw)
            tmp=directory/'records.csv.tmp'
            with tmp.open('w',newline='') as f:
                w=csv.writer(f);w.writerow(FIELDS);w.writerows(RECORD.iter_unpack(raw));f.flush();os.fsync(f.fileno())
            os.replace(tmp,directory/'records.csv')
            self._write(directory/'complete.json',json.dumps(dict(records=total,sha256=hashlib.sha256(raw).hexdigest())).encode())
        return struct.pack('<4sII',b'PDGA',token,offset)
    @staticmethod
    def _write(path,data):
        tmp=path.with_name(path.name+'.tmp')
        with tmp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
