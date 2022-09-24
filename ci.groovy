@Library('blossom-github-lib@master') 
import ipp.blossom.*

podTemplate(cloud:'sc-ipp-blossom-prod', yaml : """
apiVersion: v1
kind: Pod
metadata:
  labels:
    some-label: some-label-value
spec:
  volumes:
  - name: scratch
    nfs:
      server: ipp1-cdot01-col01
      path: /vol/scratch1/scratch.okuchaiev_blossom
  containers:
  - name: cuda
    image: nvcr.io/nvidia/pytorch:22.08-py3
    command:
    - cat
    volumeMounts:
    - name: scratch
      mountPath: /testdata
    resources:
          requests:
             nvidia.com/gpu: 1
          limits:
             nvidia.com/gpu: 1
    restartPolicy: Never
    backoffLimit: 4
    tty: true
  nodeSelector:
    kubernetes.io/os: linux
    nvidia.com/gpu_type: "A100_PCIE_40GB"
"""
)   {
      node(POD_LABEL) {
          def githubHelper
          stage('Get Token') {
              withCredentials([usernamePassword(credentialsId: 'GHAtoken', passwordVariable: 'GIT_PASSWORD', usernameVariable: 'GIT_USERNAME')]) {
                  // create new instance of helper object
                  githubHelper = GithubHelper.getInstance("${GIT_PASSWORD}", githubData)
                  
              }
              
          }
          def stageName = '' 
          try {
              currentBuild.description = githubHelper.getBuildDescription()
              container('cuda') {

                stageName = 'Code checkout'
                stage(stageName) {
                    // update status on github
                    githubHelper.updateCommitStatus("$BUILD_URL", "$stageName Running", GitHubCommitState.PENDING)
                    checkout changelog: true, poll: true, scm: [$class: 'GitSCM', branches: [[name: "pr/"+githubHelper.getPRNumber()]],
                    doGenerateSubmoduleConfigurations: false,
                    submoduleCfg: [],
                    userRemoteConfigs: [[credentialsId: 'github-token', url: githubHelper.getCloneUrl(), refspec: '+refs/pull/*/head:refs/remotes/origin/pr/*']]]              
                }

                stageName = "Code Style"
                stage(stageName) {
                        sh "apt-get update && \
                            apt-get install -y bc && \
                            pip install -r requirements/requirements_test.txt && \
                            python setup.py style && ls -l /testdata"
                }
                

                stageName = 'Installation & GPU unit tests'
                stage(stageName) {
                            sh "nvidia-smi && ./reinstall.sh release && \
                                NEMO_NUMBA_MINVER=0.53 pytest -m 'not pleasefixme and not torch_tts'"         
                }
              }
              githubHelper.updateCommitStatus("$BUILD_URL", "Complete", GitHubCommitState.SUCCESS)
          }
          catch (Exception ex){
              currentBuild.result = 'FAILURE'
              println ex
              githubHelper.updateCommitStatus("$BUILD_URL", "$stageName Failed", GitHubCommitState.FAILURE)
          }
          
      }
  }