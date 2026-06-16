local Pipeline(name, steps, trigger={}) = {
  kind: 'pipeline',
  type: 'docker',
  name: name,
  steps: steps,
  trigger: trigger,
};

[
  Pipeline('build', [
    {
      name: 'build-image',
      image: 'plugins/docker',
      depends_on: ['clone'],
      settings: {
        dockerfile: 'Dockerfile',
        registry: 'git.example.com',
        repo: 'git.example.com/youruser/rtp',
        config: { from_secret: 'dockerconfigjson' },
        tags: ['latest'],
        purge: false,
      },
    },
  ], { branch: ['main'], event: ['push'] }) + {
    image_pull_secrets: ['dockerconfigjson'],
  },
]
