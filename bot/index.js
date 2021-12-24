const assert = require('assert');
const events = require('events');
const net = require('net');

const { Client, Intents } = require('discord.js');
const { SlashCommandBuilder } = require('@discordjs/builders');
const { REST } = require('@discordjs/rest');
const { Routes } = require('discord-api-types/v9');

const discord = new Client({
	intents: [
		Intents.FLAGS.GUILDS,
		Intents.FLAGS.GUILD_MESSAGES,
	],
});

const config = require('./config.json');
try {
	Object.assign(config, require('./config.private.json'));
} catch (e) {
	console.warn('No config.private.json found');
}

const DISCORD_MAX_MESSAGE_LENGTH = 2000;

const rest = new REST({ version: '9' }).setToken(config.token);

const commands = [
	new SlashCommandBuilder().setName('killall')
		.setDescription('Destroys the current session and restarts it.'),
	new SlashCommandBuilder().setName('press')
		.setDescription('Press a key.')
		.addStringOption(option =>
			option.setName('key')
				.setDescription('The key to press')
				.setRequired(true)),
].map(command => command.toJSON());

const discordEscape = function(message) {
	// https://stackoverflow.com/a/39543625
	return message.replace(/(\*|_|`|~|\\|>)/g, '\\$1');
};

// ncurses/test $ ./list_keys -tx linux
const LONGKEYS = [
	[['Escape', 'Esc'], 0x1b],
	[['Home'], { norm: '\x1bOH', mod: '\x1b[1;{m}H' }],
	[['End'], { norm: '\x1bOF', mod: '\x1b[1;{m}F' }],
	[['Insert'], '\x1b[2~'],
	[['Delete', 'Del'], { norm: '\x1b[3~', mod: '\x1b[3;{n}~' }],
	[['Backspace'], { norm: 0x7F, ctrl: 0x8 }],
	[['Tab'], 0x9],
	// This should actually be CR, not LF, but comm replace
	// all LF with CR so it doesn't really matter
	[['Enter'], 0xa],
	[['Space'], { char: ' ' }],
	[['Page Up', 'PgUp'], '\x1b[5~'],
	[['Page Down', 'PgDown'], '\x1b[6~'],
	[['Up'], { norm: '\x1bOA', mod: '\x1b[1;{m}A' }],
	[['Down'], { norm: '\x1bOB', mod: '\x1b[1;{m}B' }],
	[['Right'], { norm: '\x1bOC', mod: '\x1b[1;{m}C' }],
	[['Left'], { norm: '\x1bOD', mod: '\x1b[1;{m}D' }],

	// [['F1'], { norm: '\x1bOP', mod: '\x1b[1;{m}P' }],
	// [['F2'], { norm: '\x1bOQ', mod: '\x1b[1;{m}Q' }],
	// [['F3'], { norm: '\x1bOR', mod: '\x1b[1;{m}R' }],
	// [['F4'], { norm: '\x1bOS', mod: '\x1b[1;{m}S' }],
	[['F1'], { norm: '\x1b[[A', mod: '\x1b[[{m}A' }],
	[['F2'], { norm: '\x1b[[B', mod: '\x1b[[{m}B' }],
	[['F3'], { norm: '\x1b[[C', mod: '\x1b[[{m}C' }],
	[['F4'], { norm: '\x1b[[D', mod: '\x1b[[{m}D' }],
	[['F5'], { norm: '\x1b[[E', mod: '\x1b[[{m}E' }],
	[['F6'], { norm: '\x1b[17~', mod: '\x1b[17;{m}~' }],
	[['F7'], { norm: '\x1b[18~', mod: '\x1b[18;{m}~' }],
	[['F8'], { norm: '\x1b[19~', mod: '\x1b[19;{m}~' }],
	[['F9'], { norm: '\x1b[20~', mod: '\x1b[20;{m}~' }],
	[['F10'], { norm: '\x1b[21~', mod: '\x1b[21;{m}~' }],
	[['F11'], { norm: '\x1b[23~', mod: '\x1b[23;{m}~' }],
	[['F12'], { norm: '\x1b[24~', mod: '\x1b[24;{m}~' }],
];

const LONGKEY_NAMES = [];
for (const [names] of LONGKEYS)
	LONGKEY_NAMES.push(...names);

const KEY_REGEXP = new RegExp(
	'^((?:Ctrl-|Alt-|Shift-)*)([a-zA-Z0-9`\\-=[\\]\\\\;\',./~!@#$%^&*()_+{}|:"<>?]|(?:' + LONGKEY_NAMES.join('|') + '))$',
	'i'
);

const SHIFTMAP = ['`1234567890-=[]\\;\',./', '~!@#$%^&*()_+{}|:"<>?'];

const parseKey = function(key) {
	const regexResult = KEY_REGEXP.exec(key);
	if (!regexResult)
		return;

	const parseDef = {
		ctrl: false,
		alt: false,
		shift: false,
		name: null,
		def: null,
	};

	if (regexResult[1]?.length) {
		let modifiers = regexResult[1];
		assert(modifiers.charAt(modifiers.length - 1) === '-');
		modifiers = modifiers.substring(0, modifiers.length - 1);

		for (let modifier of modifiers.split('-')) {
			modifier = modifier.toLowerCase();
			assert(parseDef[modifier] === false || parseDef[modifier] === true);

			parseDef[modifier] = true;
		}
	}

	for (const [names, def] of LONGKEYS) {
		for (const name of names) {
			if (name.toLowerCase() === regexResult[2].toLowerCase()) {
				parseDef.name = name;
				parseDef.def = def;
				break;
			}
		}

		if (parseDef.def !== null)
			break;
	}

	if (parseDef.def === null) {
		assert(regexResult[2].length === 1);
		parseDef.def = { char: regexResult[2] };
	};

	const result = {};

	result.canon = '';
	if (parseDef.ctrl)
		result.canon += 'Ctrl-';
	if (parseDef.alt)
		result.canon += 'Alt-';
	if (parseDef.shift)
		result.canon += 'Shift-';
	result.canon += parseDef.name || regexResult[2];

	if (parseDef.def.char !== undefined) {
		let char = parseDef.def.char;

		if (parseDef.shift) {
			const shiftmapInd = SHIFTMAP[0].indexOf(char);

			if (shiftmapInd >= 0) {
				char = SHIFTMAP[1][shiftmapInd];
			} else {
				const code = char.charCodeAt();
				// lowercase a-z
				if (code >= 0x61 && code <= 0x7a)
					char = String.fromCharCode(code - 0x20);
			}
		}

		if (parseDef.ctrl) {
			const code = char.charCodeAt();
			char = String.fromCharCode(code % 0x20);
		}

		if (parseDef.alt)
			result.ansi = '\x1b' + char;
		else
			result.ansi = char;
	} else {
		let def = parseDef.def;
		let inhibitAlt = false;

		if (typeof def === 'object') {
			if (parseDef.ctrl && def.ctrl) {
				def = def.ctrl;
			} else if (def.mod && (parseDef.ctrl || parseDef.alt || parseDef.shift)) {
				let mod = 1;
				if (parseDef.ctrl) mod += 4;
				if (parseDef.alt) mod += 2;
				if (parseDef.shift) mod += 1;
				def = def.mod.replace('{m}', mod.toString());
				inhibitAlt = true;
			} else {
				def = def.norm;
			}
		}

		if (typeof def === 'string') {
			result.ansi = def;
		} else if (typeof def === 'number') {
			if (parseDef.ctrl)
				def %= 0x20;
			result.ansi = String.fromCharCode(def);
		}

		if (parseDef.alt && !inhibitAlt)
			result.ansi = '\x1b' + result.ansi;
	}

	return result;
};

(async function() {
	discord.login(config.token);
	await events.once(discord, 'ready');
	discord.user.setActivity('bash $');

	const sessions = new Map();

	// This is the main session reactor for each channel,
	// handling channel messages, slash commands, and data from its comm.
	const handleChannel = function(channel) {
		let comm, connectedResolve;
		const connected = new Promise((resolve, reject) => {
			connectedResolve = resolve;
		});

		let destroying = false;
		const destroy = function() {
			if (!destroying) {
				destroying = true;
				handleChannel(channel);
				if (comm) comm.destroy();
			}
		};

		let sendChatChain = connected;

		let modified = false;
		let prevDirect, prevDirectContentReal, prevDirectOff;
		let prevPrompt, prevPromptContentReal, prevDisplay;
		const recvComm = async function(obj) {
			let editPrevMessage;
			if (obj.type === 'PROMPT') {
				let payload = obj.payload;
				payload = discordEscape(payload);

				if (!payload.match(/^\s*$/))
					prevPrompt = await channel.send(payload);
				else
					prevPrompt = null;
				prevPromptContentReal = payload;
				prevDirect = prevDisplay = null;
				modified = false;
			} else if (obj.type === 'DIRECT') {
				let payload = obj.payload;
				const lastCharIsLF = payload.charAt(payload.length - 1) === '\n';

				if (lastCharIsLF)
					payload = payload.substring(0, payload.length - 1);

				// NOTE: comm guarantees the payload won't have \r\n
				if (payload.charAt(0) === '\r') {
					prevDirectOff = 0;
					while (payload.charAt(0) === '\r')
						payload = payload.substring(1);
				}

				payload = discordEscape(payload);

				let prevDirectOffFirstLine = null;
				if (prevDirect) {
					// Can't use prevDirect.content because spacing are trimmed
					let prevPayload = prevDirectContentReal;
					let prevLast, editPrev;

					// Split at first line in payload, first line should
					// go to editing last line of prev message, if possible
					const firstLF = payload.indexOf('\n');
					if (firstLF < 0) {
						editPrev = payload;
						payload = '';
					} else {
						editPrev = payload.substring(0, firstLF);
						payload = payload.substring(firstLF + 1);
					}

					const prevPayloadLastLF = prevPayload.lastIndexOf('\n');
					if (prevPayloadLastLF < 0) {
						prevLast = prevPayload;
						prevPayload = '';
					} else {
						prevLast = prevPayload.substring(prevPayloadLastLF + 1);
						prevPayload = prevPayload.substring(0, prevPayloadLastLF + 1);
					}

					let editPrevAgg = '';
					for (const [index, pseudo] of editPrev.split('\r').entries()) {
						if (index) {
							editPrevAgg = pseudo + editPrevAgg.substring(pseudo.length);
							prevDirectOff = pseudo.length;
						} else {
							editPrevAgg = prevLast.substring(0, prevDirectOff) +
								pseudo +
								prevLast.substring(prevDirectOff + pseudo.length);
							prevDirectOff += pseudo.length;
						}
					}
					editPrev = editPrevAgg;
					prevDirectOffFirstLine = prevDirectOff;

					if (firstLF < 0)
						payload = editPrev;
					else
						payload = editPrev + '\n' + payload;

					if (!prevPayload.match(/^\s*$/))
						await prevDirect.edit(prevPayload);
					else if (modified)
						await prevDirect.delete();
					else
						editPrevMessage = prevDirect;

					prevDirect = prevDirectContentReal = null;
				}

				if (payload.length) {
					const payloadLines = payload.split('\n');
					for (const [index, line] of payloadLines.entries()) {
						let aggline = '';
						for (const pseudo of line.split('\r')) {
							aggline = pseudo + aggline.substring(pseudo.length);
							prevDirectOff = pseudo.length;
						}

						if (!index && prevDirectOffFirstLine !== null)
							prevDirectOff = prevDirectOffFirstLine;

						payloadLines[index] = aggline;
					}

					payload = payloadLines.join('\n');
				}

				let lastMessage, lastMessagePayload;
				while (payload.length) {
					if (payload.length > DISCORD_MAX_MESSAGE_LENGTH) {
						let splitPoint = payload.lastIndexOf('\n', DISCORD_MAX_MESSAGE_LENGTH - 1);
						if (splitPoint < 0)
							splitPoint = DISCORD_MAX_MESSAGE_LENGTH;

						lastMessagePayload = payload.substring(0, splitPoint);
						payload = payload.substring(splitPoint);
					} else {
						lastMessagePayload = payload;
						payload = '';
					}

					if (!editPrevMessage) {
						if (!lastMessagePayload.match(/^\s*$/))
							lastMessage = await channel.send(lastMessagePayload);
						else
							lastMessage = null;
					} else {
						if (!lastMessagePayload.match(/^\s*$/)) {
							lastMessage = await editPrevMessage.edit(lastMessagePayload);
						} else {
							await editPrevMessage.delete();
							lastMessage = null;
						}

						editPrevMessage = null;
					}
				}

				prevDirect = lastCharIsLF ? null : lastMessage;
				prevDirectContentReal = lastCharIsLF ? null : lastMessagePayload;
				prevPrompt = prevDisplay = null;
				modified = false;
			} else if (obj.type === 'DISPLAY') {
				const payload = '```\n' + obj.payload + '\n```';

				if (prevDisplay)
					prevDisplay = await prevDisplay.edit(payload);
				else
					prevDisplay = await channel.send(payload);

				prevPrompt = prevDirect = null;
			} else if (obj.type === 'UPLOAD') {
				const payload = Buffer.from(obj.payload, 'base64');

				await channel.send({
					files: [{
						attachment: payload,
						name: 'upload',
					}],
				});

				modified = true;
			}
		};

		// This promise chaining here is needed here because we need to
		// serialize when a drain is needed.
		let sendCommChain = connected;
		const sendComm = function(obj) {
			console.log(obj);
			const pkt = JSON.stringify(obj);

			sendCommChain = sendCommChain.then(async function() {
				if (destroying)
					return;

				if (!comm.write(pkt + '\n'))
					await events.once(comm, 'drain');
			});
		};

		// Exported properties and methods
		const session = {
			_markModified: function() {
				modified = true;
			},
			input: function(message) {
				sendChatChain = sendChatChain.then(async () => {
					await connected;
					sendComm({ type: 'INPUT', payload: message });

					if (prevPrompt) {
						await prevPrompt.delete();
						prevPrompt = null;
						await channel.send(prevPromptContentReal + message);
					}
				});
			},
			killall: function() {
				destroy();
			},
		};

		sessions.set(channel.id, session);

		connected.then(() => {
			let buf = '';
			comm.on('data', chunk => {
				buf += chunk;

				while (true) {
					const lineInd = buf.indexOf('\n');
					if (lineInd < 0)
						break;

					const pkt = buf.substring(0, lineInd);
					buf = buf.substring(lineInd + 1);

					const obj = JSON.parse(pkt);
					sendChatChain = sendChatChain.then(async () => recvComm(obj));
				}
			});

			comm.on('close', () => destroy());
		});

		// Everything ready, now connect.
		(async function() {
			for (let i = 0; ; i++) {
				await new Promise(resolve => setTimeout(resolve, 1000));
				if (destroying)
					return;

				console.log(`Establishing comm for channel #${channel.name}, attempt ${i + 1}`);
				comm = new net.Socket();

				try {
					comm.connect(49813);
					await events.once(comm, 'ready');
				} catch (e) {
					comm.destroy();
					await new Promise(resolve => setTimeout(resolve, 4000));
					continue;
				}

				console.log(`Channel #${channel.name} ready!`);
				connectedResolve();
				break;
			}
		}());
	};

	for (const [guildId, channels] of Object.entries(config.channels)) {
		await rest.put(
			Routes.applicationGuildCommands(discord.user.id, guildId),
			// Routes.applicationCommands(discord.user.id),
			{ body: commands },
		);

		for (const channelId of channels)
			handleChannel(await discord.channels.fetch(channelId));
	}

	discord.on('messageCreate', async message => {
		const session = sessions.get(message.channelId);
		if (!session)
			return;

		if (message.author.bot)
			return;

		session._markModified();

		const content = message.cleanContent;

		if (content.charAt(0) === '!')
			session.input(message.cleanContent.substring(1) + '\n');
	});

	discord.on('interactionCreate', async interaction => {
		if (!interaction.isCommand())
			return;

		const session = sessions.get(interaction.channelId);

		if (!session) {
			await interaction.reply('This channel is not handled.');
			return;
		}

		session._markModified();

		if (interaction.commandName === 'killall') {
			await interaction.reply('Destroying and restarting session...');
			session.killall();
		} else if (interaction.commandName === 'press') {
			const key = interaction.options.get('key').value;
			const parse = parseKey(key);

			if (!parse) {
				await interaction.reply(`Unknown key: ${discordEscape(key)}`);
				return;
			}

			await interaction.reply(`Pressing ${discordEscape(parse.canon)}`);
			session.input(parse.ansi);
		}
	});
}());
