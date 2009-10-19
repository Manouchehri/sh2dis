#!/usr/bin/env python


"""
TODO:
- make some sort of note with the code about both resolved and unresolved
  branch targets.
"""


import csv, segment, struct
from sh2opcodes import opcodes


# Opcodes that use delayed branching; an legal instruction should follow.
delayed_branchers = ('bra','braf','jmp','rte','rts')

# Opcodes that branch based on the contents of a register.
register_branchers = ('braf','bsrf','jsr','jmp')

# Opcodes that branch based on a directly-referenced label.
label_branchers = ('bf','bf/s','bra','bsr','bt','bt/s')


class DataField(segment.SegmentData):
    """Metaclass for SH2 data types."""

    def get_instruction(self):
        """A GAS-compatible string representation for this data type."""
        name = self.__class__.__name__.lower().replace('field', '')
        val = '.%s 0x%%0%dX' % (name, (self.width * 2))
        return val % self.extra

    def get_label(self):
        if self.label is None and len(self.references) > 0:
            meta = self.model.get_location(self.location - 1)
            if meta is not None and meta.label is not None:
                return '%s+%d' % (meta.label, self.location-meta.location)
            return 'unk_%X' % self.location
        if self.label is not None:
            return self.label
        return None


class ByteField(DataField):
    """Defines a single byte."""

    def __init__(self, *args, **kwargs):
        kwargs['width'] = 1
        DataField.__init__(self, *args, **kwargs)
        try:
            self.extra = ord(self.model.get_phys(self.location, 1))
        except segment.SegmentError:
            self.extra = None


class WordField(DataField):
    """Defines a "word" of two bytes."""

    def __init__(self, *args, **kwargs):
        kwargs['width'] = 2
        DataField.__init__(self, *args, **kwargs)
        try:
            bytes = self.model.get_phys(self.location, 2)
            self.extra = struct.unpack('>H', bytes)[0]
        except segment.SegmentError:
            self.extra = None


class LongField(DataField):
    """Defines a "long-word" of four bytes."""

    def __init__(self, *args, **kwargs):
        kwargs['width'] = 4
        DataField.__init__(self, *args, **kwargs)
        try:
            bytes = self.model.get_phys(self.location, 4)
            self.extra = struct.unpack('>L', bytes)[0]
        except segment.SegmentError:
            self.extra = None

        # Make us a reference if we refer to a legitimate address.
        try:
            if self.model.get_location(self.extra) is None:
                create_reference(self.location, self.extra, self.model)
        except segment.SegmentError:
            pass

    def get_instruction(self):
        name = self.__class__.__name__.lower().replace('field', '')
        try:
            meta = self.model.get_location(self.extra)
            if meta is not None:
                text = meta.get_label()
                if text is None:
                    raise segment.SegmentError
            else:
                raise segment.SegmentError
        except segment.SegmentError:
            text = '0x%%0%dX' % (self.width * 2)
            text = text % self.extra
        return '.%s %s' % (name, text)


class CodeField(DataField):
    """Defines a single assembly instruction."""

    def __init__(self, *args, **kwargs):
        DataField.__init__(self, *args, **kwargs)

    def get_instruction(self):
        if 'label' in self.extra.text:
            meta = self.model.get_location(self.extra.args['target'])
            if meta is not None:
                target = meta.get_label()
                if target is None:
                    target = '0x%X' % self.extra.args['target']
            else:
                target = 'sub_%X' % self.extra.args['target']
            text = self.extra.text.replace('label', target)
        else:
            text = self.extra.text
        return text

    def get_label(self):
        if self.label is None and len(self.references) > 0:
            return 'sub_%X' % self.location
        if self.label is not None:
            return self.label
        return None

    def generate_comments(self):
        comments = DataField.generate_comments(self)
        if 'target' in self.extra.args and self.extra.args['target'] is not None:
            meta = self.model.get_location(self.extra.args['target'])
            if meta is not None:
                target = meta.get_label()
                if target is not None:
                    comments.append('REF: %s' % target)
        return comments


class NullField(segment.SegmentData):
    """Defines a section of unused space."""

    def __init__(self, *args, **kwargs):
        segment.SegmentData.__init__(self, *args, **kwargs)

    def get_instruction(self):
        return '.org %#X' % (self.location + self.width)


class AssemblyError(StandardError):
    pass


def create_reference(referer, location, model, metatype=ByteField):
    meta = model.get_location(location)
    if meta is None:
        meta = metatype(location=location, model=model)
        model.set_location(meta)
    if referer is not None:
        meta.references.append(referer)
    return meta


def parse_args(instruction, opcode):
    op = { }
    if opcode['m'][0] != 0:
        op['m'] = (instruction & opcode['m'][0]) >> opcode['m'][1]
    else:
        op['m'] = None
    if opcode['n'][0] != 0:
        op['n'] = (instruction & opcode['n'][0]) >> opcode['n'][1]
    else:
        op['n'] = None
    if opcode['imm'][0] != 0:
        op['imm'] = (instruction & opcode['imm'][0]) >> opcode['imm'][1]
    else:
        op['imm'] = None
    if opcode['disp'] != 0:
        op['disp'] = instruction & opcode['disp']
    else:
        op['disp'] = None
    return op


def calculate_disp_target(opcode, args, pc):
    disp = args['disp']
    if disp is not None:
        # 12-bit disp values are signed.
        #sign = 0x800 if opcode['disp'] & 0xF00 != 0 else 0
        sign = 0
        if opcode['disp'] & 0xF00 != 0:
            sign = 0x800
        elif opcode['disp'] & 0xF0 != 0:
            sign = 0x80
        elif opcode['disp'] & 0xF != 0:
            sign = 0x8
        else:
            sign = 0

        # 1-, 2-, or 4-byte multiplier determination.
        if opcode['cmd'][-2:] == '.b':
            disp_mult = 1
        elif opcode['cmd'][-2:] == '.l' or opcode['cmd'] == 'mova':
            disp_mult = 4
        else:
            disp_mult = 2

        if disp & sign != 0 and opcode['cmd'][0:3] != 'mov':
            target = -((sign << 1) - ((disp - sign) * disp_mult))
        else:
            target = disp * disp_mult

        # Long disp values require a PC alignment mask.
        if disp_mult == 4:
            target += (pc & 0xFFFFFFFC) + 4
        else:
            target += pc + 4

        args['target'] = target
        args['disp'] *= disp_mult
    else:
        args['target'] = disp


def track_registers(opcode, args, location, registers, model):
    """Track register assignments."""
    n = args['n']
    if n is not None:
        if opcode['cmd'][:3] == 'mov':
            if args['m'] is None:
                if args['imm'] is not None:
                    registers[n] = args['imm']
                    return
                if args['disp'] is not None:
                    target = args['target']
                    meta = model.get_location(target)
                    if meta is None:
                        if opcode['cmd'][-2:] == '.l' or opcode['cmd'] == 'mova':
                            meta_type = LongField
                        elif opcode['cmd'][-2:] == '.w':
                            meta_type = WordField
                        else:
                            meta_type = ByteField
                        meta = create_reference(location, target, model, meta_type)
                    registers[n] = meta.extra
                    return

        # Any action on a target register that we can't explicitly
        # calculate invalidates any current value. Register estimation
        # is a best-effort kind of thing.
        registers[n] = None
    elif opcode['args'][-3:] == ',r0':
        registers[0] = None


def lookup_instruction(instruction):
    """Perform a basic lookup of the instruction in our registry."""
    # TODO: brute force sucks, we can do much better here.
    for opcode in opcodes:
        instbits, instmask = opcode['opmask']
        if instruction & instmask == instbits:
            args = parse_args(instruction, opcode)
            return opcode, args
    raise AssemblyError, 'no matching instruction for %#x' % instruction


def disasm_single(instruction, pc, registers, model):
    """Disassemble a single instruction."""
    class CodeExtra:
        pass

    opcode, args = lookup_instruction(instruction)
    calculate_disp_target(opcode, args, pc)
    track_registers(opcode, args, pc, registers, model)
    extra = CodeExtra()
    extra.text = ' '.join((opcode['cmd'], opcode['args'] % args))
    extra.opcode = opcode
    extra.args = args
    return CodeField(location=pc, width=2, extra=extra, model=model)


def disassemble(location, reference, model):
    """Given a memory model and a location within it, disassemble."""
    work_queue = [ (location, reference), ]

    while len(work_queue) > 0:
        location, reference = work_queue.pop()
        registers = [None,] * 16
        branching = False
        branch_countdown = 0

        # Quick check to make sure we haven't already processed this location.
        meta = model.get_location(location)
        if isinstance(meta, CodeField):
            if reference not in meta.references:
                meta.references.append(reference)
            continue

        while not branching or branch_countdown >= 0:
            try:
                orig = model.get_location(location)
                bytes = model.get_phys(location, 2)
            except segment.SegmentError:
                break

            instruction = struct.unpack('>H', bytes)[0]
            code = disasm_single(instruction, location, registers, model)

            # Handle register-based branches.
            if code.extra.opcode['cmd'] in register_branchers:
                if registers[code.extra.args['m']] is not None:
                    work_queue.append((registers[code.extra.args['m']], location))
                # TODO: Make some kind of note about unresolved branches.
                #else:
                #    print 'unresolved branch at 0x%x' % location
            if code.extra.opcode['cmd'] in label_branchers:
                work_queue.append((code.extra.args['target'], location))

            if reference is not None:
                code.references.append(reference)
                reference = None
            if orig is not None:
                code.label = orig.label

            model.set_location(code)

            if code.extra.opcode['cmd'] in delayed_branchers:
                branch_countdown = 1
                branching = True
                if code.extra.args['target'] is not None:
                    work_queue.append((code.extra.args['target'], location))
            if branching:
                branch_countdown -= 1

            location += 2

if __name__ == '__main__':
    print 'no tests yet'
