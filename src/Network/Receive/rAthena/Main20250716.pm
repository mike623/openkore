# rAthena built for the 2025-07-16 main client (Ragnarok Offline / nostalRE).
# Packets parse as the kRO 2021-11-03 RE client. For XKore 2, each packet name is bound to the
# ID the real server last used, so packets replayed to the client match what the server itself
# sends (otherwise the first ID listed for a name wins, e.g. 0069 instead of 0AC4 for the server
# list). All instances share one binding table: XKore 2's account, char and map servers each have
# their own parser, and they must see what the connection to the real server learned.
package Network::Receive::rAthena::Main20250716;
use strict;
use base qw(Network::Receive::kRO::RagexeRE_2021_11_03);

our %lut;

sub new {
	my ($class) = @_;
	my $self = $class->SUPER::new(@_);
	%lut = (%{$self->{packet_lut} || {}}, %lut);
	$self->{packet_lut} = \%lut;
	return $self;
}

sub parse {
	my ($self, $msg) = @_;
	if (length($msg) >= 2) {
		my $switch = sprintf('%04X', unpack('v', $msg));
		my $handler = $self->{packet_list}{$switch};
		$lut{$handler->[0]} = $switch if $handler;
	}
	return $self->SUPER::parse(@_[1 .. $#_]);
}

sub reconstruct {
	my ($self, $args) = @_;
	# rAthena puts the slot counts in 082D; the 006B XKore 2 replays has them zero unless filled in,
	# and a client then shows no character slots.
	if ($args->{switch} eq 'received_characters_info' && !$args->{total_slot}) {
		my $slots = $Globals::charSvrSet{normal_slot} || $Globals::charSvrSet{valid_slot} || 15;
		$args->{total_slot} = $slots;
		$args->{premium_start_slot} //= $slots;
		$args->{premium_end_slot} //= $slots;
	}
	return $self->SUPER::reconstruct($args);
}

1;
